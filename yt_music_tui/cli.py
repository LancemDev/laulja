from __future__ import annotations

import asyncio
import sys

from .auth.browser_flow import BrowserAuthFlow
from .auth.service import AuthService
from .auth.session import AuthSession
from .config import AppConfig
from .services.audio.player import AudioPlayerService
from .services.cover_art_service import CoverArtService
from .services.lyrics_service import LyricsService
from .services.music_service import MusicService

"""Mirrors Program.cs: CLI arg handling, then either runs the TUI or a one-shot auth command."""

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


async def _with_spinner(message: str, awaitable):
    """Runs `awaitable` while printing a plain-terminal spinner — for the startup work that
    happens before the Textual app exists yet (so no widget can show a loading state)."""

    async def spin() -> None:
        i = 0
        while True:
            print(f"\r{_SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]} {message}", end="", flush=True)
            i += 1
            await asyncio.sleep(0.1)

    spin_task = asyncio.ensure_future(spin())
    try:
        return await awaitable
    finally:
        spin_task.cancel()
        try:
            await spin_task
        except asyncio.CancelledError:
            pass
        print("\r" + " " * (len(message) + 2) + "\r", end="", flush=True)


def main() -> None:
    args = sys.argv[1:]
    config = AppConfig.load()
    auth_service = AuthService(
        cookies_path=config.cookies_path,
        session_path=config.session_path,
        geographical_location=config.geographical_location,
        validate_with_api=config.validate_auth_on_startup,
        headers_auth_path=config.headers_auth_path,
    )

    if args:
        code = asyncio.run(_run_cli(args, auth_service, config))
        sys.exit(code)

    asyncio.run(_run_tui(auth_service, config))


async def _run_tui(auth_service: AuthService, config: AppConfig) -> None:
    session = await _ensure_authenticated(auth_service, config, force_login=False)

    from ytmusicapi import YTMusic

    from .auth import client_factory

    client: YTMusic = await _with_spinner(
        "Connecting to YouTube Music…",
        asyncio.to_thread(client_factory.create, session, config.headers_auth_path),
    )
    music = MusicService(client)
    player = AudioPlayerService(music)
    lyrics = LyricsService()
    cover_art = CoverArtService()

    from .ui.app import MusicApp

    app = MusicApp(config, music, player, lyrics, cover_art, session)
    try:
        await app.run_async()
    finally:
        await player.dispose()
        await lyrics.aclose()
        await cover_art.aclose()


async def _ensure_authenticated(
    auth: AuthService, config: AppConfig, force_login: bool
) -> AuthSession:
    if not force_login and not config.force_browser_login:
        existing = await asyncio.to_thread(auth.load)
        if existing.is_authenticated:
            return existing

        if not config.prompt_login_on_startup:
            print(f"Starting without auth ({existing.status_label}): {existing.status_detail}")
            return existing

        print(f"Not authenticated ({existing.status_label}). Starting browser sign-in…")

    flow = BrowserAuthFlow(auth)
    return await flow.run()


async def _run_cli(args: list[str], auth: AuthService, config: AppConfig) -> int:
    command = args[0]

    if command in ("--login", "login"):
        try:
            session = await _ensure_authenticated(auth, config, force_login=True)
            _print_session(session)
            return 0 if session.is_authenticated else 1
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("Login cancelled.", file=sys.stderr)
            return 130

    if command in ("--auth-status", "auth-status"):
        session = await asyncio.to_thread(auth.load)
        _print_session(session)
        return 0 if session.is_authenticated else 1

    if command in ("--import-cookies", "import-cookies"):
        if len(args) < 2:
            print("Usage: yt-music-tui --import-cookies <path-to-cookies>", file=sys.stderr)
            return 2
        try:
            imported = auth.import_cookies(args[1])
            print(imported.status_detail)
            print("Validating against YouTube Music library…")
            validated = await asyncio.to_thread(auth.validate, imported)
            _print_session(validated)
            return 0 if validated.is_authenticated else 1
        except Exception as ex:
            print(str(ex), file=sys.stderr)
            return 1

    if command in ("--help", "-h", "help"):
        _print_help()
        return 0

    print(f"Unknown argument: {command}", file=sys.stderr)
    _print_help()
    return 2


def _print_session(session: AuthSession) -> None:
    print(f"Status:   {session.status_label}")
    print(f"Detail:   {session.status_detail}")
    print(f"Cookies:  {session.cookies_path}")
    print(f"Count:    {len(session.cookies)}")
    print(f"Geo:      {session.geographical_location}")
    print(f"Visitor:  {'(none)' if not session.visitor_data else 'set'}")
    print(f"PoToken:  {'(none)' if not session.po_token else 'set'}")


def _print_help() -> None:
    print(
        """yt-music-tui (Python)

  (no args)                 Start TUI (opens browser login if needed)
  --login                   Force browser sign-in flow
  --auth-status             Load cookies and validate auth
  --import-cookies <path>   Copy cookie file into config and validate
  --help                    Show this help

On first run (or expired cookies), the app opens YouTube Music sign-in
directly in your browser, then auto-detects the session from your
browser's cookies. If that fails, it asks you to paste the Cookie
header instead.

Env:
  YT_MUSIC_COOKIES          Cookie file path
  YT_MUSIC_SESSION          Session file path (visitorData/poToken/geo)
  YT_MUSIC_GEO              Geographical location code (default US)
  YT_MUSIC_HEADERS_AUTH     Path to a headers_auth.json from `ytmusicapi browser`
  YT_MUSIC_SKIP_LOGIN=1     Do not open browser login on startup
  YT_MUSIC_SKIP_AUTH_CHECK=1  Skip the startup library-access check
"""
    )


if __name__ == "__main__":
    main()
