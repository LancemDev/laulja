from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

from . import client_factory
from . import paths as auth_paths
from . import session as session_store
from .cookies import Cookie, has_auth_cookies, parse_file, parse_header
from .session import AuthSession, AuthStatus


def _session_identity(cookies: List[Cookie]) -> Optional[str]:
    """The value that identifies *which* account a cookie jar belongs to.

    The timestamp/CSRF cookies rotate constantly, so they can't be compared across jars —
    but the PAPISID/SAPISID identity stays the same for a given signed-in account.
    """
    by_name = {c.name: c.value for c in cookies}
    return by_name.get("__Secure-3PAPISID") or by_name.get("SAPISID")


class AuthService:
    def __init__(
        self,
        cookies_path: Optional[str] = None,
        session_path: Optional[str] = None,
        geographical_location: str = "US",
        validate_with_api: bool = True,
        headers_auth_path: Optional[str] = None,
    ):
        self.cookies_path = cookies_path or str(auth_paths.default_cookies_path())
        self.session_path = session_path or str(auth_paths.default_session_path())
        self.geographical_location = geographical_location
        self.validate_with_api = validate_with_api
        self.headers_auth_path = headers_auth_path

    def load(self) -> AuthSession:
        file = session_store.load_or_default(self.session_path)
        geo = file.geographical_location or self.geographical_location
        cookies_path = self._resolve_cookies_path(file.cookies_path)

        if not Path(cookies_path).is_file():
            return AuthSession(
                cookies=[],
                visitor_data=file.visitor_data,
                po_token=file.po_token,
                geographical_location=geo,
                cookies_path=cookies_path,
                status=AuthStatus.MISSING,
                status_detail=f"No cookies at {cookies_path}",
            )

        try:
            cookies = parse_file(cookies_path)
        except Exception as ex:
            return AuthSession(
                cookies=[],
                visitor_data=file.visitor_data,
                po_token=file.po_token,
                geographical_location=geo,
                cookies_path=cookies_path,
                status=AuthStatus.INVALID_COOKIES,
                status_detail=str(ex),
            )

        if not cookies or not has_auth_cookies(cookies):
            return AuthSession(
                cookies=cookies,
                visitor_data=file.visitor_data,
                po_token=file.po_token,
                geographical_location=geo,
                cookies_path=cookies_path,
                status=AuthStatus.INVALID_COOKIES,
                status_detail="Cookies found but missing SAPISID / __Secure-3PAPISID (sign-in required)",
            )

        session = AuthSession(
            cookies=cookies,
            visitor_data=file.visitor_data,
            po_token=file.po_token,
            geographical_location=geo,
            cookies_path=cookies_path,
            status=AuthStatus.AUTHENTICATED,
            status_detail=f"{len(cookies)} cookies loaded",
        )

        if not self.validate_with_api:
            return session
        validated = self.validate(session)
        if validated.is_authenticated:
            return validated

        # YouTube rotates the timestamp/CSRF cookies (__Secure-3PSIDTS, SIDCC, ...) while you
        # browse. The app stores a snapshot, so those copies eventually go stale even though
        # the account's identity cookies are still valid. Before asking for a fresh sign-in,
        # merge in what the user's browser currently has — but only for the same account.
        refreshed = self._refresh_from_browser(validated)
        return refreshed or validated

    def _refresh_from_browser(self, session: AuthSession) -> Optional[AuthSession]:
        """Re-import the browser's current YouTube cookies to refresh rotated ones.

        Only succeeds when the browser is signed into the same account (the identity cookie
        matches) and the merged jar validates, so this never silently switches accounts.
        Returns None if there's nothing usable, leaving the caller to prompt for sign-in.
        """
        # A headers_auth.json takes precedence in client_factory.create(), so merged
        # cookies.txt entries would be ignored and the same stale headers would be validated.
        if self.headers_auth_path:
            return None

        try:
            from . import browser_cookies

            detected, source = browser_cookies.try_read_youtube_cookies()
        except Exception:
            return None

        if not detected or not has_auth_cookies(detected):
            return None
        identity = _session_identity(session.cookies)
        if identity is None or _session_identity(detected) != identity:
            return None

        # Keep the stored jar's order (cookies_to_header's last-one-wins dedup relies on it)
        # and replace matching names with the browser's newer values; append anything new.
        merged = {c.name: c for c in session.cookies}
        for cookie in detected:
            merged[cookie.name] = cookie
        cookies = list(merged.values())

        candidate = AuthSession(
            cookies=cookies,
            visitor_data=session.visitor_data,
            po_token=session.po_token,
            geographical_location=session.geographical_location,
            cookies_path=session.cookies_path,
            status=AuthStatus.AUTHENTICATED,
        )
        validated = self.validate(candidate)
        if not validated.is_authenticated:
            return None

        saved = self.save_cookies(cookies)
        saved.status_detail = (
            f"Refreshed {len(cookies)} cookies from {source} → {self.cookies_path}"
            if source
            else f"Refreshed {len(cookies)} cookies → {self.cookies_path}"
        )
        return saved

    def validate(self, session: AuthSession) -> AuthSession:
        try:
            client = client_factory.create(session, self.headers_auth_path)
            # get_account_info() requires a real signed-in account menu and raises clearly
            # when it isn't there; get_library_songs() would silently return [] for bogus
            # cookies instead of failing, which makes a useless validation signal.
            client.get_account_info()

            return AuthSession(
                cookies=session.cookies,
                visitor_data=session.visitor_data,
                po_token=session.po_token,
                geographical_location=session.geographical_location,
                cookies_path=session.cookies_path,
                status=AuthStatus.AUTHENTICATED,
                status_detail="Account access OK",
            )
        except Exception as ex:
            return AuthSession(
                cookies=session.cookies,
                visitor_data=session.visitor_data,
                po_token=session.po_token,
                geographical_location=session.geographical_location,
                cookies_path=session.cookies_path,
                status=AuthStatus.VALIDATION_FAILED,
                status_detail=str(ex),
            )

    def sign_out(self) -> None:
        """Removes locally saved cookies/session so the next launch requires signing in again."""
        Path(self.cookies_path).unlink(missing_ok=True)
        Path(self.session_path).unlink(missing_ok=True)

    def save_cookies(self, cookies: List[Cookie]) -> AuthSession:
        if not cookies:
            raise ValueError("No cookies were found.")
        if not has_auth_cookies(cookies):
            raise ValueError("No usable session found (missing SAPISID / __Secure-3PAPISID).")

        auth_paths.ensure_config_directory()
        payload = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
            for c in cookies
        ]
        auth_paths.secure_write_text(self.cookies_path, json.dumps(payload, indent=2))

        return self._persist_cookie_session(cookies, f"Saved {len(cookies)} cookies → {self.cookies_path}")

    def save_cookies_from_header(self, cookie_header: str) -> AuthSession:
        cookies = parse_header(cookie_header)
        if not cookies:
            raise ValueError("No cookies could be parsed from the header.")
        if not has_auth_cookies(cookies):
            raise ValueError(
                "Header parsed but is missing SAPISID / __Secure-3PAPISID. "
                "Copy the Cookie header while signed into music.youtube.com."
            )

        auth_paths.ensure_config_directory()
        auth_paths.secure_write_text(self.cookies_path, cookie_header.strip() + "\n")

        return self._persist_cookie_session(cookies, f"Saved {len(cookies)} cookies → {self.cookies_path}")

    def import_cookies(self, source_path: str) -> AuthSession:
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"Cookie file not found: {source_path}")

        # Verify parse before copying.
        cookies = parse_file(src)
        if not cookies:
            raise ValueError("No cookies could be parsed from the file.")
        if not has_auth_cookies(cookies):
            raise ValueError(
                "File parsed but is missing SAPISID / __Secure-3PAPISID. "
                "Export cookies while signed into music.youtube.com."
            )

        auth_paths.ensure_config_directory()
        shutil.copyfile(src, self.cookies_path)
        auth_paths.restrict_to_owner(self.cookies_path)

        file = session_store.load_or_default(self.session_path)
        file.cookies_path = self.cookies_path
        if not file.geographical_location:
            file.geographical_location = self.geographical_location
        session_store.save(self.session_path, file)

        return AuthSession(
            cookies=cookies,
            visitor_data=file.visitor_data,
            po_token=file.po_token,
            geographical_location=file.geographical_location,
            cookies_path=self.cookies_path,
            status=AuthStatus.AUTHENTICATED,
            status_detail=f"Imported {len(cookies)} cookies → {self.cookies_path}",
        )

    def save_session_tokens(
        self,
        visitor_data: Optional[str],
        po_token: Optional[str],
        geographical_location: Optional[str] = None,
    ) -> None:
        auth_paths.ensure_config_directory()
        file = session_store.load_or_default(self.session_path)
        file.visitor_data = visitor_data or file.visitor_data
        file.po_token = po_token or file.po_token
        file.geographical_location = (
            geographical_location or file.geographical_location or self.geographical_location
        )
        file.cookies_path = file.cookies_path or self.cookies_path
        session_store.save(self.session_path, file)

    def _persist_cookie_session(self, cookies: List[Cookie], status_detail: str) -> AuthSession:
        file = session_store.load_or_default(self.session_path)
        file.cookies_path = self.cookies_path
        if not file.geographical_location:
            file.geographical_location = self.geographical_location
        session_store.save(self.session_path, file)

        return AuthSession(
            cookies=cookies,
            visitor_data=file.visitor_data,
            po_token=file.po_token,
            geographical_location=file.geographical_location,
            cookies_path=self.cookies_path,
            status=AuthStatus.AUTHENTICATED,
            status_detail=status_detail,
        )

    def _resolve_cookies_path(self, configured_path: Optional[str]) -> str:
        from_env = os.environ.get("YT_MUSIC_COOKIES")
        if from_env:
            return str(Path(from_env).resolve())
        if configured_path:
            return str(Path(configured_path).resolve())
        return str(Path(self.cookies_path).resolve())
