"""Downloads audio tracks for offline playback.

Tracks are saved into the machine's Downloads folder (``Downloads/laulja`` by default) as
``<title> - <artist>.<ext>`` using YouTube's best audio-only stream — no re-encoding, so the
file is exactly what YouTube serves and keeps its original quality.

Up to :attr:`max_concurrent` downloads run at once; later requests wait on a semaphore. Each
download reports progress (0.0–1.0) and status (queued / downloading / done / failed) via
yt-dlp's progress hook, so the UI can show a live indicator while files arrive.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yt_dlp

from ..auth.cookies import to_netscape
from ..models import Track


def _sanitize(value: str) -> str:
    """Make a value safe to use in a filename on any OS."""
    value = re.sub(r'[<>:"/\\|?*]', "", value).strip()
    return value or "untitled"


def _available_js_runtimes() -> Dict[str, Dict[str, Any]]:
    """YouTube's stream URLs are behind a JS challenge yt-dlp solves with an external runtime."""
    import shutil

    runtimes = ("deno", "node", "bun", "quickjs")
    return {name: {} for name in runtimes if shutil.which(name)}


class DownloadService:
    def __init__(
        self,
        cookies,
        download_dir: Optional[Path] = None,
        max_concurrent: int = 3,
    ) -> None:
        self._cookies = list(cookies or [])
        # The machine's main Downloads folder, tucked into a ``laulja`` subdirectory so
        # downloads don't litter the root of Downloads.
        self._dir = Path(download_dir) if download_dir else Path.home() / "Downloads" / "laulja"
        self._dir.mkdir(parents=True, exist_ok=True)
        # Up to ``max_concurrent`` downloads run at once; later requests wait on the semaphore.
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        # Guards the _progress / _status dicts, which are written from yt-dlp's worker thread
        # (a plain threading.Lock, since that hook runs synchronously and can't await).
        self._lock = threading.Lock()
        # Per-track progress 0.0–1.0, updated by yt-dlp's progress hook.
        self._progress: Dict[str, float] = {}
        # Per-track status: "queued", "downloading", "done", "failed", "idle".
        self._status: Dict[str, str] = {}
        # Completed download results: track_id -> final Path, so a duplicate 'd' on an
        # in-flight track can return the existing path without re-downloading.
        self._results: Dict[str, Path] = {}

    # -- queries ------------------------------------------------------------

    def base_name(self, track: Track) -> str:
        return f"{_sanitize(track.title)} - {_sanitize(track.artist)}"

    def is_downloaded(self, track: Track) -> bool:
        """True if a completed file for this track already exists (any extension)."""
        base = self._dir / self.base_name(track)
        return any(self._dir.glob(f"{base.name}.*"))

    def existing_path(self, track: Track) -> Optional[Path]:
        """The path of the completed file, if any — used to play a track from disk."""
        base = self._dir / self.base_name(track)
        matches = list(self._dir.glob(f"{base.name}.*"))
        return matches[0] if matches else None

    def get_progress(self, track_id: str) -> float:
        """Download progress for `track_id`, 0.0–1.0."""
        with self._lock:
            return self._progress.get(track_id, 0.0)

    def get_status(self, track_id: str) -> str:
        """Download status for `track_id`: "queued", "downloading", "done", "failed", "idle"."""
        with self._lock:
            return self._status.get(track_id, "idle")

    def active_count(self) -> int:
        """Tracks currently being downloaded (semaphore acquired, in-flight)."""
        with self._lock:
            return sum(1 for s in self._status.values() if s == "downloading")

    def queued_count(self) -> int:
        """Tracks waiting for a semaphore slot."""
        with self._lock:
            return sum(1 for s in self._status.values() if s == "queued")

    def result_path(self, track_id: str) -> Optional[Path]:
        """The final path of a completed download, if we recorded one."""
        with self._lock:
            return self._results.get(track_id)

    # -- download -----------------------------------------------------------

    async def download(self, track: Track) -> Path:
        """Download `track`'s audio and return the final file path.

        Waits on the semaphore if ``max_concurrent`` downloads are already in flight.
        Raises on failure so the caller can record the error. Already-completed tracks
        return their existing path without re-downloading.
        """
        track_id = track.id
        if self.is_downloaded(track):
            path = self.existing_path(track)
            if path is not None:
                return path

        # Mark as queued immediately so the UI can show it before the semaphore slot opens.
        with self._lock:
            existing_status = self._status.get(track_id, "idle")
            if existing_status in ("queued", "downloading"):
                # Already in flight — wait for that one instead of starting a duplicate.
                pass
            else:
                self._status[track_id] = "queued"
                self._progress[track_id] = 0.0

        async with self._semaphore:
            with self._lock:
                if self._status.get(track_id) == "failed":
                    raise RuntimeError(f"Previous download failed for {track.title}.")
                self._status[track_id] = "downloading"
                self._progress[track_id] = 0.0

            try:
                final = await asyncio.to_thread(self._download_file, track, track_id)
                return final
            except Exception:
                with self._lock:
                    self._status[track_id] = "failed"
                    self._progress[track_id] = 0.0
                raise

    def _write_cookies(self) -> Optional[str]:
        if not self._cookies:
            return None
        fd, path = tempfile.mkstemp(prefix="laulja-dl-cookies-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(to_netscape(self._cookies))
        return path

    def _download_file(self, track: Track, track_id: str) -> Path:
        base = self._dir / self.base_name(track)
        # Unique temp stem — no file is created here, so the post-download glob only
        # finds what yt-dlp actually wrote (no empty .part file to confuse it).
        stem = self._dir / f".laulja-dl-{uuid.uuid4().hex}"
        cookie_path = self._write_cookies()
        downloaded: Optional[Path] = None
        try:
            runtimes = _available_js_runtimes()
            # Strictly audio-only: no /best fallback, which can pull a video stream
            # (WebM video) when a pure audio format isn't available. Prefer m4a/aac
            # (universally playable, never mistaken for video) over webm.
            opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
                "noplaylist": True,
                "outtmpl": str(stem) + ".%(ext)s",
                "progress_hooks": [lambda d: self._progress_hook(d, track_id)],
            }
            if runtimes:
                opts["js_runtimes"] = runtimes
            if cookie_path:
                opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://music.youtube.com/watch?v={track_id}"])
            matches = list(self._dir.glob(stem.name + ".*"))
            if not matches:
                raise RuntimeError(f"yt-dlp did not produce a file for {track.title}.")
            downloaded = matches[0]
            final = base.with_suffix(downloaded.suffix)
            if final.exists():
                final.unlink()
            downloaded.rename(final)
            if not final.exists():
                raise RuntimeError(f"Download did not produce {final}.")
            with self._lock:
                self._status[track_id] = "done"
                self._progress[track_id] = 1.0
                self._results[track_id] = final
            return final
        finally:
            if downloaded is not None:
                for extra in self._dir.glob(stem.name + ".*"):
                    if extra != downloaded:
                        try:
                            extra.unlink()
                        except OSError:
                            pass
            if cookie_path:
                try:
                    os.remove(cookie_path)
                except OSError:
                    pass

    def _progress_hook(self, d: Dict[str, Any], track_id: str) -> None:
        """yt-dlp progress hook: records downloaded bytes / total as a 0.0–1.0 fraction."""
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            progress = downloaded / total if total else 0.0
            progress = max(0.0, min(1.0, progress))
        elif d.get("status") == "finished":
            progress = 1.0
        else:
            return
        # Called from yt-dlp's worker thread; simple dict assignment is GIL-atomic, but we
        # take the lock to be safe since app.py reads these dicts from the main thread.
        with self._lock:
            self._progress[track_id] = progress
