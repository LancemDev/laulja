"""Downloads audio tracks for offline playback.

Tracks are saved into the machine's Downloads folder (``Downloads/laulja`` by default) as
``<title> - <artist>.<ext>`` using YouTube's best audio-only stream — no re-encoding, so the
file is exactly what YouTube serves and keeps its original quality. Only one download runs at
a time; later requests wait their turn. A partial download is written to a hidden temp file
and renamed into place only once it's complete, so nothing ever looks finished that isn't.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
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
    ) -> None:
        self._cookies = list(cookies or [])
        # The machine's main Downloads folder, tucked into a ``laulja`` subdirectory so
        # downloads don't litter the root of Downloads.
        self._dir = Path(download_dir) if download_dir else Path.home() / "Downloads" / "laulja"
        self._dir.mkdir(parents=True, exist_ok=True)
        # One download at a time; later requests wait on the lock.
        self._lock = asyncio.Lock()

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

    # -- download -----------------------------------------------------------

    async def download(self, track: Track) -> Path:
        """Download `track`'s audio and return the final file path.

        Waits its turn if another download is in flight. Raises on failure so the caller can
        record the error. Already-completed tracks return their existing path without
        re-downloading.
        """
        if self.is_downloaded(track):
            path = self.existing_path(track)
            if path is not None:
                return path
        async with self._lock:
            # Re-check once serialized — another coroutine may have finished it first.
            if self.is_downloaded(track):
                path = self.existing_path(track)
                if path is not None:
                    return path
            return await asyncio.to_thread(self._download_file, track)

    def _write_cookies(self) -> Optional[str]:
        if not self._cookies:
            return None
        fd, path = tempfile.mkstemp(prefix="laulja-dl-cookies-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(to_netscape(self._cookies))
        return path

    def _download_file(self, track: Track) -> Path:
        base = self._dir / self.base_name(track)
        # Unique temp stem — no file is created here, so the post-download glob only
        # finds what yt-dlp actually wrote (no empty .part file to confuse it).
        stem = self._dir / f".laulja-dl-{uuid.uuid4().hex}"
        cookie_path = self._write_cookies()
        downloaded: Optional[Path] = None
        try:
            runtimes = _available_js_runtimes()
            opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio/best",
                "noplaylist": True,
                "outtmpl": str(stem) + ".%(ext)s",
            }
            if runtimes:
                opts["js_runtimes"] = runtimes
            if cookie_path:
                opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://music.youtube.com/watch?v={track.id}"])
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
