from __future__ import annotations

import os
from pathlib import Path

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
    config_directory().mkdir(parents=True, exist_ok=True)
