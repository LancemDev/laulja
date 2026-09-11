# yt-music-tui-py

A Python port of [yt-music-tui](../yt-music-tui) — a terminal UI for YouTube Music with real
playback, a live FFT bar visualizer, and synced lyrics.

Same feature set and keybindings as the original C#/Ratatui.cs project, rebuilt on idiomatic
Python:

| Concern              | Original (C#)                          | This port (Python)              |
|----------------------|-----------------------------------------|----------------------------------|
| TUI framework        | Ratatui.cs                              | [Textual](https://textual.textualize.io/) |
| YouTube Music API    | YouTubeMusicAPI                         | [ytmusicapi](https://ytmusicapi.readthedocs.io/) |
| Stream URL resolution| YouTubeMusicAPI + YouTubeSessionGenerator (needs Node.js) | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (no Node.js needed) |
| Audio decode/output  | ffmpeg → pw-play/paplay/aplay           | ffmpeg → PortAudio (`sounddevice`), falling back to pw-play/paplay/aplay on Linux |
| Bar visualizer FFT   | hand-rolled radix-2 FFT                 | numpy                            |
| Cookie decryption    | hand-rolled AES via `System.Security.Cryptography` | `cryptography`, same algorithm |
| Lyrics               | LRCLIB                                  | same                             |

## Install

```bash
pipx install yt-music-tui-py   # or: uv tool install yt-music-tui-py
```

`pipx`/`uv tool` give it its own isolated environment — no venv to manage, `pipx upgrade
yt-music-tui-py` to update. Works on Linux, macOS, and Windows.

To hack on it from a checkout instead:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**System dependency:** `ffmpeg` for decoding — install it via your OS's package manager
(`apt install ffmpeg`, `brew install ffmpeg`, `scoop install ffmpeg`, …).

Audio output uses [PortAudio](https://www.portaudio.com/) (via the `sounddevice` package,
installed automatically) and works out of the box on all three platforms. On Linux, if
PortAudio's system library (`libportaudio2`) isn't installed, it automatically falls back to
shelling out to `pw-play` (PipeWire), `paplay` (PulseAudio), or `aplay` (ALSA) instead — so at
least one of those four needs to be available.

Automatic browser-cookie detection (see below) only runs on Linux; macOS and Windows fall back
to the manual cookie-paste flow, which works the same everywhere.

## Run

```bash
yt-music-tui                       # start the TUI (opens browser login if needed)
yt-music-tui --login               # force the browser sign-in flow
yt-music-tui --auth-status         # load cookies and validate auth
yt-music-tui --import-cookies path # copy a cookies.txt into config and validate
yt-music-tui --help
```

On first run (or once cookies expire) it opens `music.youtube.com` in your browser, then tries
to auto-detect the resulting session from your browser's cookie store (Chrome, Chromium, Brave,
Edge, Firefox). If that fails, it falls back to pasting the `Cookie` request header copied from
DevTools.

## Keybindings

```
Tab        cycle focus between Tracks, Queue, and Playlists
j/k, ↑/↓   move selection
Enter      play selected track / playlist / queue item
Space      play/pause
n / p      next / previous track
x          remove the selected track from the Queue panel
/          search — Tracks or Playlists, whichever panel is focused (Esc cancels, Enter runs it)
Esc        back to library from search results
f          cycle fullscreen: normal → cover+bar → lyrics → normal
c          collapse the sidebar
m          toggle minimalism (strip panel borders and the hint bar)
q          quit
```

Playing an individual track (library or search) starts a YouTube Music radio/mix seeded from
it — a continuous queue of similar-vibe songs — rather than just the track alone; playing a
playlist queues it in order instead. The Queue panel shows what's playing next: `Enter` jumps
straight to a track, `x` removes one (the currently-playing track can't be removed this way —
skip to it instead).

## Config

Same environment variables as the original:

| Variable                    | Meaning                                             |
|------------------------------|------------------------------------------------------|
| `YT_MUSIC_COOKIES`           | Cookie file path                                     |
| `YT_MUSIC_SESSION`           | Session file path (geo/visitorData/poToken)          |
| `YT_MUSIC_GEO`                | Geographical location code (default `US`)            |
| `YT_MUSIC_SKIP_LOGIN=1`      | Don't open the browser login flow on startup          |
| `YT_MUSIC_SKIP_AUTH_CHECK=1` | Skip the startup library-access check                 |

Plus one new to this port:

| Variable                | Meaning                                                                 |
|--------------------------|--------------------------------------------------------------------------|
| `YT_MUSIC_HEADERS_AUTH` | Path to a `headers_auth.json` generated by `ytmusicapi browser` — use this as a fallback if the built-in cookie-based auth ever gets rejected by YouTube. |

Config/cookies/session files live under `$XDG_CONFIG_HOME/yt-music-tui-py` (or
`~/.config/yt-music-tui-py`) — a separate directory from the original C# app, so both can
coexist without clobbering each other's session.

## Notable differences from the original

- **No Node.js dependency.** The C# original shells out to a local Node.js process
  (`YouTubeSessionGenerator`) to solve YouTube's BotGuard challenge and mint a `PoToken` before
  every session. This port instead resolves stream URLs with `yt-dlp`, which handles that
  internally — one less runtime dependency to install.
- **Textual instead of a hand-rolled redraw loop.** The original polls for terminal events and
  redraws immediately-mode each frame. This port uses Textual's reactive/async widget model —
  same visual layout and behavior, different plumbing under the hood.
- **Auth validation** calls `ytmusicapi`'s `get_account_info()` rather than a library listing,
  since an unauthenticated `ytmusicapi` client silently returns an empty library instead of
  raising — `get_account_info()` gives a reliable signed-in/not-signed-in signal instead.

## Project layout

```
yt_music_tui/
  cli.py                  CLI entry point (mirrors Program.cs)
  config.py                env-driven AppConfig
  models.py                Track/Playlist/Lyrics dataclasses
  auth/                    cookie parsing, browser cookie auto-detect, sign-in flow
  services/
    music_service.py       ytmusicapi + yt-dlp wrapper
    lyrics_service.py       LRCLIB client
    audio/                  ffmpeg pipeline, audio sink, FFT bar visualizer
  ui/
    app.py                  Textual App: layout, keybindings, state sync
    state.py                shared AppState
    widgets.py              header/tracks/playlists/cover/bar/lyrics panels
```
