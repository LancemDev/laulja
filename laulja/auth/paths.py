from __future__ import annotations

import os
from pathlib import Path
from typing import Union

APP_FOLDER_NAME = "laulja"
COOKIES_FILE_NAME = "cookies.txt"
SESSION_FILE_NAME = "session.json"


def config_directory() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / APP_FOLDER_NAME


def default_cookies_path() -> Path:
    return config_directory() / COOKIES_FILE_NAME


def default_session_path() -> Path:
    return config_directory() / SESSION_FILE_NAME


def ensure_config_directory() -> None:
    directory = config_directory()
    directory.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(directory)


def secure_write_text(path: Union[str, Path], content: str) -> None:
    """Writes `content` to `path` and restricts it to the owner (0600) — cookies.txt and
    session.json hold a live YouTube session cookie, equivalent to a password, so another local
    account on a shared machine shouldn't be able to read it just because it landed in this
    config directory. Applied after writing (not via a permissive-then-fixed race) since these
    are single, short config files rather than something a concurrent reader is racing to open."""
    p = Path(path)
    p.write_text(content)
    restrict_to_owner(p)


def restrict_to_owner(path: Union[str, Path]) -> None:
    p = Path(path)
    try:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    except OSError:
        pass  # e.g. read-only filesystem — not worth failing the save over
