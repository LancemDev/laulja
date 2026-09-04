from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from . import paths as auth_paths
from .cookies import Cookie


class AuthStatus(Enum):
    MISSING = "missing"
    INVALID_COOKIES = "invalid_cookies"
    AUTHENTICATED = "authenticated"
    VALIDATION_FAILED = "validation_failed"


_STATUS_LABELS = {
    AuthStatus.AUTHENTICATED: "signed in",
    AuthStatus.MISSING: "not signed in",
    AuthStatus.INVALID_COOKIES: "invalid cookies",
    AuthStatus.VALIDATION_FAILED: "auth failed",
}


@dataclass
class AuthSession:
    cookies: List[Cookie] = field(default_factory=list)
    visitor_data: Optional[str] = None
    po_token: Optional[str] = None
    geographical_location: str = "US"
    cookies_path: Optional[str] = None
    status: AuthStatus = AuthStatus.MISSING
    status_detail: Optional[str] = None

    @property
    def is_authenticated(self) -> bool:
        return self.status == AuthStatus.AUTHENTICATED

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, "unknown")


@dataclass
class SessionFile:
    geographical_location: str = "US"
    visitor_data: Optional[str] = None
    po_token: Optional[str] = None
    cookies_path: Optional[str] = None

    def to_json(self) -> dict:
        data = {
            "geographicalLocation": self.geographical_location,
            "visitorData": self.visitor_data,
            "poToken": self.po_token,
            "cookiesPath": self.cookies_path,
        }
        return {k: v for k, v in data.items() if v is not None}

    @staticmethod
    def from_json(data: dict) -> "SessionFile":
        return SessionFile(
            geographical_location=data.get("geographicalLocation", "US"),
            visitor_data=data.get("visitorData"),
            po_token=data.get("poToken"),
            cookies_path=data.get("cookiesPath"),
        )


def load_or_default(path: str | Path) -> SessionFile:
    p = Path(path)
    if not p.is_file():
        return SessionFile()
    try:
        return SessionFile.from_json(json.loads(p.read_text()))
    except Exception:
        return SessionFile()


def save(path: str | Path, session: SessionFile) -> None:
    auth_paths.ensure_config_directory()
    Path(path).write_text(json.dumps(session.to_json(), indent=2))
