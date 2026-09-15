from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import chrome_crypto
from .cookies import Cookie, create_cookie, has_auth_cookies

"""
Auto-detects a signed-in YouTube Music session by reading cookies directly out of
the user's local browser profiles (Firefox, Chrome, Chromium, Brave, Edge) — the same
approach tools like yt-dlp's --cookies-from-browser use. Linux only for now.
Mirrors BrowserCookieReader.cs.
"""


@dataclass
class _Candidate:
    label: str
    db_path: Path
    is_firefox: bool
    secret_tool_apps: List[str]


def _find_candidates() -> List[_Candidate]:
    home = Path.home()
    config = home / ".config"
    candidates: List[_Candidate] = []

    def add_chrome_family(label: str, root: Path, secret_apps: List[str]) -> None:
        if not root.is_dir():
            return
        for profile_dir in root.iterdir():
            if not profile_dir.is_dir():
                continue
            name = profile_dir.name
            if name != "Default" and not name.startswith("Profile ") and name != "Guest Profile":
                continue
            db = profile_dir / "Cookies"
            if db.is_file():
                candidates.append(_Candidate(label, db, False, secret_apps))

    def add_firefox(label: str, root: Path) -> None:
        if not root.is_dir():
            return
        for profile_dir in root.iterdir():
            db = profile_dir / "cookies.sqlite"
            if db.is_file():
                candidates.append(_Candidate(label, db, True, []))

    add_chrome_family("Chrome", config / "google-chrome", ["chrome", "Chrome", "Google Chrome"])
    add_chrome_family("Chromium", config / "chromium", ["chromium", "Chromium"])
    add_chrome_family("Brave", config / "BraveSoftware" / "Brave-Browser", ["brave", "Brave"])
    add_chrome_family(
        "Edge", config / "microsoft-edge", ["microsoft-edge", "Microsoft Edge", "Chromium"]
    )
    add_firefox("Firefox", home / ".mozilla" / "firefox")
    add_firefox("Zen", home / ".zen")

    return candidates


def _copy_to_temp(db_path: Path) -> Path:
    temp = Path(tempfile.gettempdir()) / f"ytmusic-cookies-{uuid.uuid4().hex}.sqlite"
    shutil.copyfile(db_path, temp)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass

    wal = Path(str(db_path) + "-wal")
    if wal.is_file():
        try:
            wal_temp = Path(str(temp) + "-wal")
            shutil.copyfile(wal, wal_temp)
            os.chmod(wal_temp, 0o600)
        except OSError:
            pass  # best effort: without the WAL, very recent cookie writes may be missing

    return temp


def _delete_temp(temp: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(temp) + suffix).unlink()
        except OSError:
            pass


def _looks_decrypted(value: str) -> bool:
    return all(ord(ch) >= 0x20 or ch == "\t" for ch in value)


def _read_firefox_cookies(db_path: Path) -> List[Cookie]:
    temp = _copy_to_temp(db_path)
    try:
        conn = sqlite3.connect(f"file:{temp}?mode=ro", uri=True)
        try:
            # Ordered oldest-to-newest so that when a name collides after domain normalization
            # (e.g. a leftover row from a previous sign-in), cookies_to_header's last-one-wins
            # dedup keeps the most recently set value rather than an arbitrary one.
            cur = conn.execute(
                "SELECT host, name, value, path, isSecure FROM moz_cookies "
                "WHERE host LIKE '%youtube.com' ORDER BY creationTime ASC"
            )
            return [
                create_cookie(name, value, host, path or "/", bool(is_secure))
                for host, name, value, path, is_secure in cur.fetchall()
            ]
        finally:
            conn.close()
    finally:
        _delete_temp(temp)


def _read_chrome_cookies(db_path: Path, secret_tool_apps: List[str]) -> List[Cookie]:
    temp = _copy_to_temp(db_path)
    try:
        conn = sqlite3.connect(f"file:{temp}?mode=ro", uri=True)
        try:
            # Ordered oldest-to-newest so that when a name collides after domain normalization
            # (e.g. a leftover row from a previous sign-in), cookies_to_header's last-one-wins
            # dedup keeps the most recently set value rather than an arbitrary one.
            cur = conn.execute(
                "SELECT host_key, name, path, encrypted_value, is_secure FROM cookies "
                "WHERE host_key LIKE '%youtube.com' ORDER BY last_update_utc ASC"
            )
            key: Optional[bytes] = None
            cookies: List[Cookie] = []
            for host, name, path, encrypted, is_secure in cur.fetchall():
                if key is None:
                    key = chrome_crypto.derive_key(secret_tool_apps)
                value = chrome_crypto.decrypt(bytes(encrypted), key)
                if value is None or not _looks_decrypted(value):
                    continue
                cookies.append(create_cookie(name, value, host, path or "/", bool(is_secure)))
            return cookies
        finally:
            conn.close()
    finally:
        _delete_temp(temp)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def try_read_youtube_cookies() -> Tuple[Optional[List[Cookie]], Optional[str]]:
    """Returns (cookies, source_label), or (None, None) if nothing usable was found."""
    if platform.system() != "Linux":
        return None, None

    candidates = sorted(_find_candidates(), key=lambda c: _safe_mtime(c.db_path), reverse=True)

    for candidate in candidates:
        try:
            cookies = (
                _read_firefox_cookies(candidate.db_path)
                if candidate.is_firefox
                else _read_chrome_cookies(candidate.db_path, candidate.secret_tool_apps)
            )
        except Exception:
            continue

        if cookies and has_auth_cookies(cookies):
            return cookies, candidate.label

    return None, None
