"""Downloads audio tracks for offline playback.

Tracks are saved into the machine's Downloads folder (``Downloads/laulja`` by default) as
``<title> - <artist>.mp3``. YouTube's best audio-only stream is decoded to plain MP3 (with
ID3 title/artist tags) so the files play anywhere, including standalone MP3 players.

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
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    # Windows ignores trailing dots/spaces, so strip them to keep names predictable there.
    value = value.strip().rstrip(".").strip()
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
        # Clear temp files left behind by an interrupted download (app closed mid-transfer).
        self._cleanup_stale_temp_files()
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
        # In-flight download tasks keyed by track id: a duplicate 'd' awaits the same task
        # instead of starting a second download. Only touched from the event loop thread.
        self._tasks: Dict[str, asyncio.Future] = {}

    # -- queries ------------------------------------------------------------

    def base_name(self, track: Track) -> str:
        return f"{_sanitize(track.title)} - {_sanitize(track.artist)}"

    def _matches(self, base_name: str) -> list[Path]:
        """Files named ``base_name.<ext>``.

        Matched with a literal prefix check rather than a glob so titles containing glob
        metacharacters (``[``, ``?``, ...) are found instead of misinterpreted as a pattern.
        """
        prefix = base_name + "."
        try:
            return [p for p in self._dir.iterdir() if p.name.startswith(prefix)]
        except OSError:
            return []

    def _cleanup_stale_temp_files(self) -> None:
        """Remove hidden temp files from a previous run that was killed mid-download."""
        try:
            for path in self._dir.glob(".laulja-dl-*"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def is_downloaded(self, track: Track) -> bool:
        """True if a completed file for this track already exists."""
        return bool(self._matches(self.base_name(track)))

    def existing_path(self, track: Track) -> Optional[Path]:
        """The path of the completed file, if any — used to play a track from disk."""
        matches = self._matches(self.base_name(track))
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
        return their existing path without re-downloading, and repeated calls for a track
        that is already queued/downloading share the in-flight task instead of starting
        a duplicate.
        """
        track_id = track.id
        task = self._tasks.get(track_id)
        if task is not None:
            return await asyncio.shield(task)
        if self.is_downloaded(track):
            path = self.existing_path(track)
            if path is not None:
                return path

        task = asyncio.ensure_future(self._run_download(track))
        self._tasks[track_id] = task
        task.add_done_callback(lambda done: self._finish_task(track_id, task, done))
        return await asyncio.shield(task)

    def _finish_task(self, track_id: str, task: asyncio.Future, done: asyncio.Future) -> None:
        if self._tasks.get(track_id) is task:
            self._tasks.pop(track_id, None)
        # Mark the exception as retrieved so a failure doesn't also log an "exception was
        # never retrieved" warning when every awaiting caller was cancelled.
        if not done.cancelled():
            done.exception()

    async def _run_download(self, track: Track) -> Path:
        track_id = track.id
        # Mark as queued immediately so the UI can show it before the semaphore slot opens.
        with self._lock:
            self._status[track_id] = "queued"
            self._progress[track_id] = 0.0

        async with self._semaphore:
            with self._lock:
                self._status[track_id] = "downloading"
                self._progress[track_id] = 0.0

            try:
                return await asyncio.to_thread(self._download_file, track, track_id)
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
        # Unique temp stem — no file is created here, so the post-download lookup only
        # finds what yt-dlp actually wrote (no empty .part file to confuse it).
        stem = self._dir / f".laulja-dl-{uuid.uuid4().hex}"
        cookie_path = self._write_cookies()
        try:
            runtimes = _available_js_runtimes()
            # Strictly audio-only (``bestaudio`` never selects a video stream), decoded to
            # plain MP3 by ffmpeg below so the file plays on any device, including basic
            # MP3 players. Metadata is embedded so players show the real title/artist.
            opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio",
                "noplaylist": True,
                "outtmpl": str(stem) + ".%(ext)s",
                "progress_hooks": [lambda d: self._progress_hook(d, track_id)],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    },
                    {"key": "FFmpegMetadata"},
                ],
            }
            if runtimes:
                opts["js_runtimes"] = runtimes
            if cookie_path:
                opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://music.youtube.com/watch?v={track_id}"])
            matches = self._matches(stem.name)
            if not matches:
                raise RuntimeError(f"yt-dlp did not produce a file for {track.title}.")
            downloaded = matches[0]
            # Plain concatenation, NOT Path.with_suffix(): with_suffix() treats a dot inside
            # the title (e.g. "Song (feat. X) - Artist") as the extension separator and
            # truncates everything after it.
            final = self._dir / (self.base_name(track) + downloaded.suffix)
            # replace() overwrites any previous copy atomically.
            downloaded.replace(final)
            if not final.exists():
                raise RuntimeError(f"Download did not produce {final}.")
            with self._lock:
                self._status[track_id] = "done"
                self._progress[track_id] = 1.0
                self._results[track_id] = final
            return final
        finally:
            # Always remove the temp stems — including a partial file when the download
            # failed — so nothing but finished tracks is ever left in the folder.
            for extra in self._matches(stem.name):
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
