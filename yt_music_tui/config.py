from __future__ import annotations

import os
from dataclasses import dataclass


def _env_flag_disabled(name: str) -> bool:
    return os.environ.get(name) == "1"


@dataclass(frozen=True)
class AppConfig:
    app_name: str = "YT Music TUI (Python)"
    tick_ms: int = 50
    geographical_location: str = "US"
    cookies_path: str | None = None
    session_path: str | None = None
    headers_auth_path: str | None = None
    validate_auth_on_startup: bool = True
    prompt_login_on_startup: bool = True
    force_browser_login: bool = False

    @staticmethod
    def load() -> "AppConfig":
        return AppConfig(
            cookies_path=os.environ.get("YT_MUSIC_COOKIES"),
            session_path=os.environ.get("YT_MUSIC_SESSION"),
            headers_auth_path=os.environ.get("YT_MUSIC_HEADERS_AUTH"),
            geographical_location=os.environ.get("YT_MUSIC_GEO", "US"),
            validate_auth_on_startup=not _env_flag_disabled("YT_MUSIC_SKIP_AUTH_CHECK"),
            prompt_login_on_startup=not _env_flag_disabled("YT_MUSIC_SKIP_LOGIN"),
        )
