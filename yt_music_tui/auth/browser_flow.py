from __future__ import annotations

import asyncio
import shutil
import subprocess
import webbrowser
from typing import Optional

from . import browser_cookies
from .service import AuthService
from .session import AuthSession

"""
Interactive sign-in for the TUI: opens music.youtube.com directly (it redirects to Google's
own current sign-in flow if needed), then auto-detects the resulting session cookie from the
user's browser profile. Falls back to a terminal paste prompt if auto-detection doesn't find
anything. Mirrors BrowserAuthFlow.cs.
"""

SIGN_IN_URL = "https://music.youtube.com/"

_KNOWN_BROWSER_BINARIES = [
    "zen",
    "brave-browser",
    "brave",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "firefox",
    "microsoft-edge",
]


def open_browser(url: str) -> None:
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass

    for binary in _KNOWN_BROWSER_BINARIES:
        path = shutil.which(binary)
        if path:
            try:
                subprocess.Popen([path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                continue

    print(f"Open this URL manually: {url}")


class BrowserAuthFlow:
    def __init__(self, auth: AuthService):
        self._auth = auth

    async def run(self) -> AuthSession:
        print()
        print("Authentication required for YouTube Music.")
        print("Press Enter to open YouTube Music in your browser…")
        await _wait_for_enter()

        open_browser(SIGN_IN_URL)

        print()
        print("Sign in there if prompted, then make sure the tab lands on music.youtube.com.")
        print("Press Enter here once you're signed in…")
        await _wait_for_enter()

        print("Looking for your session in Chrome, Chromium, Brave, Edge, and Firefox…")
        detected, source = await asyncio.to_thread(browser_cookies.try_read_youtube_cookies)

        imported: Optional[AuthSession] = None
        if detected:
            try:
                print(f"Found a signed-in session via {source}.")
                imported = self._auth.save_cookies(detected)
            except Exception as ex:
                print(f"Auto-detected session looked invalid: {ex}")

        if imported is None:
            print("" if detected else "Couldn't auto-detect a session from your browsers.")
            print(
                "Open DevTools (F12) → Network → any music.youtube.com request → "
                "copy the 'Cookie' request header."
            )

            for _ in range(3):
                header = await _prompt("Paste it here: ")
                try:
                    imported = self._auth.save_cookies_from_header(header or "")
                    break
                except Exception as ex:
                    print(f"{ex} Try again.")

        if imported is None:
            print("Giving up after repeated invalid input.")
            return self._auth.load()

        print("Validating with YouTube Music…")
        validated = await asyncio.to_thread(self._auth.validate, imported)

        print(
            "Signed in."
            if validated.is_authenticated
            else f"Sign-in saved but validation failed: {validated.status_detail}"
        )

        return validated


async def _wait_for_enter() -> None:
    await asyncio.to_thread(input)


async def _prompt(message: str) -> str:
    return await asyncio.to_thread(input, message)
