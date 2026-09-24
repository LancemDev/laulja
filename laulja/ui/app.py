from __future__ import annotations

import asyncio
from typing import Optional

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Input, ProgressBar

from ..auth.service import AuthService
from ..auth.session import AuthSession
from ..config import AppConfig
from ..models import Playlist, Track
from ..services.audio.player import AudioPlayerService
from ..services.cover_art_service import CoverArtService
from ..services.lyrics_service import LyricsService
from ..services.music_service import MusicService
from . import art_theme
from .state import AppState, FullScreenMode, LeftFocus, WallpaperVisual
from .widgets import (
    BarVisualizerPanel,
    CoverArtPanel,
    HeaderBar,
    LyricsPanel,
    PlayerInfoBar,
    PlaylistsPanel,
    QueuePanel,
    QuickActionsBar,
    TracksPanel,
    WallpaperPanel,
)

_LEFT_FOCUS_CYCLE = [LeftFocus.TRACKS, LeftFocus.QUEUE, LeftFocus.PLAYLISTS]
_WALLPAPER_VISUALS = list(WallpaperVisual)

# ytmusicapi's own convention for "the current account's Liked Songs" — get_playlist(id) accepts
# it exactly like any real playlist id, so it can be faked as one Playlist entry at the top of
# the Playlists panel rather than needing a separate liked-songs browsing path.
LIKED_SONGS_PLAYLIST_ID = "LM"

# Applied at startup, before any cover art has loaded to derive a real one (art_theme.py) —
# Textual's own built-in themes all default `primary` (panel borders, scrollbars) to a shade of
# blue, which reads as a stock, unthemed placeholder. Warm instead, so the very first screen
# already looks like part of this app rather than generic Textual chrome.
_DEFAULT_THEME = Theme(
    name="laulja-warm",
    primary="#cc7832",
    secondary="#8a4b2f",
    warning="#ffa62b",
    error="#ba3c5b",
    success="#4ebf71",
    accent="#ffa62b",
    foreground="#e0e0e0",
    dark=True,
)

_CSS = """
Screen {
    layout: vertical;
    background: $surface;
}

#header {
    height: 1;
    content-align: center middle;
    text-align: center;
}

#search-input {
    height: 3;
    border: round $accent;
    display: none;
}
#search-input.visible {
    display: block;
}

#body {
    height: 1fr;
}

#left {
    width: 28;
}
#left.hidden {
    display: none;
}

#tracks {
    height: 45%;
}
#queue {
    height: 30%;
}
#playlists {
    height: 25%;
}
/* Paged sidebar mode ("v"): only the focused one of tracks/queue/playlists is visible, filling
   the whole sidebar instead of its usual height-sliced share. */
#tracks.hidden, #queue.hidden, #playlists.hidden {
    display: none;
}
#tracks.fill, #queue.fill, #playlists.fill {
    height: 1fr;
}

#center {
    width: 1fr;
}
#center.hidden {
    display: none;
}

#cover {
    height: 65%;
}
#bar {
    height: 35%;
}

#lyrics {
    width: 34;
}
#lyrics.hidden {
    display: none;
}
#lyrics.fill {
    width: 1fr;
}

#player {
    height: 4;
}
#player-gauge {
    height: 1;
}

#quickactions {
    height: 1;
    color: $text-muted;
    padding: 0 1;
}

/* Minimalism mode ("m"): strip panel borders/padding and hide the hint bar, leaving just
   content — tracks/playlists/lyrics/player-info otherwise carry the same border as before. */
Screen.minimal #tracks,
Screen.minimal #playlists,
Screen.minimal #queue,
Screen.minimal #lyrics,
Screen.minimal #player-info {
    border: none;
    padding: 0;
}
Screen.minimal #quickactions {
    display: none;
}

/* Wallpaper mode ("w"): full-screen generative visuals in place of everything else — the
   sidebar/cover/lyrics/player-info panels aren't just hidden behind it, they're removed from
   layout entirely so #wallpaper (height: 1fr) fills the whole screen on its own. */
#wallpaper {
    display: none;
    width: 1fr;
    height: 1fr;
}
Screen.wallpaper #header,
Screen.wallpaper #search-input,
Screen.wallpaper #body,
Screen.wallpaper #player,
Screen.wallpaper #quickactions {
    display: none;
}
Screen.wallpaper #wallpaper {
    display: block;
}
"""


class MusicApp(App[None]):
    """Textual port of App/MusicApp.cs — same state machine and keybindings, driven by
    Textual's own async event loop instead of a hand-rolled poll/redraw loop."""

    CSS = _CSS
    TITLE = "Laulja"

    def __init__(
        self,
        config: AppConfig,
        music: MusicService,
        player: AudioPlayerService,
        lyrics: LyricsService,
        cover_art: CoverArtService,
        auth: AuthSession,
        auth_service: AuthService,
    ) -> None:
        super().__init__()
        self._config = config
        self._music = music
        self._player = player
        self._lyrics = lyrics
        self._cover_art = cover_art
        self._auth = auth
        self._auth_service = auth_service
        self.state = AppState(is_authenticated=auth.is_authenticated, auth_label=auth.status_label)
        self._lyrics_request_id = 0
        self._cover_art_request_id = 0

    def compose(self) -> ComposeResult:
        yield HeaderBar(self.state, self._config.app_name, id="header")
        yield Input(placeholder="Search…", id="search-input")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield TracksPanel(self.state, id="tracks")
                yield QueuePanel(self.state, id="queue")
                yield PlaylistsPanel(self.state, id="playlists")
            with Vertical(id="center"):
                yield CoverArtPanel(self.state, id="cover")
                yield BarVisualizerPanel(self.state, id="bar")
            yield LyricsPanel(self.state, id="lyrics")
        with Vertical(id="player"):
            yield PlayerInfoBar(self.state, id="player-info")
            yield ProgressBar(id="player-gauge", total=1000, show_eta=False, show_percentage=False)
        yield QuickActionsBar(self.state, id="quickactions")
        yield WallpaperPanel(self.state, id="wallpaper")

    async def on_mount(self) -> None:
        # Only the search Input should ever hold real focus; everywhere else our own on_key
        # drives selection (j/k/tab/etc.), matching the original's single global input handler.
        self.query_one("#search-input", Input).can_focus = False
        self.set_focus(None)

        self.register_theme(_DEFAULT_THEME)
        self.theme = _DEFAULT_THEME.name

        self._apply_layout()
        self.set_interval(self._config.tick_ms / 1000, self._on_tick)
        await self._load_initial_data()
        self.refresh_all()

    async def on_unmount(self) -> None:
        await self._player.dispose()
        await self._lyrics.aclose()
        await self._cover_art.aclose()

    # ---- data loading -------------------------------------------------

    async def _load_initial_data(self) -> None:
        s = self.state
        # Three independent network round-trips — fetched concurrently rather than one after
        # another, since each one waiting on the last was most of what made startup slow.
        # return_exceptions=True so one failing (e.g. liked songs, which is a nice-to-have)
        # doesn't take the others down with it.
        tracks, playlists, liked_ids = await asyncio.gather(
            self._music.get_library_tracks(),
            self._music.get_library_playlists(),
            self._music.get_liked_song_ids(),
            return_exceptions=True,
        )

        if isinstance(tracks, BaseException):
            s.status_message = f"Couldn't load library: {tracks}"
        else:
            s.library_tracks = tracks

        if not isinstance(liked_ids, BaseException):
            s.liked_track_ids = liked_ids

        if isinstance(playlists, BaseException):
            if not isinstance(tracks, BaseException):
                s.status_message = f"Couldn't load playlists: {playlists}"
        else:
            # "Liked Songs" behaves like any other playlist in YT Music (browsable, playable in
            # order) but isn't itself returned by get_library_playlists() — synthesize an entry
            # for it, seeded from the liked-song count already fetched above, so it shows up
            # first without a separate round-trip just to get its track count.
            liked_count = len(liked_ids) if not isinstance(liked_ids, BaseException) else 0
            liked_playlist = Playlist(id=LIKED_SONGS_PLAYLIST_ID, title="Liked Songs", track_count=liked_count)
            s.playlists = [liked_playlist, *playlists]

        if not isinstance(tracks, BaseException) and not isinstance(playlists, BaseException):
            s.status_message = (
                f"Auth OK · {self._auth.status_detail}"
                if self._auth.is_authenticated
                else f"Auth: {self._auth.status_label} · {self._auth.status_detail}"
            )

        s.is_loading_library = False

    # ---- player tick ----------------------------------------------------

    def _on_tick(self) -> None:
        s = self.state
        before = (s.position_seconds, s.is_playing, s.now_playing.id if s.now_playing else None)
        self._player.tick()
        # tick() only *schedules* next_track() (via ensure_future) rather than running it
        # synchronously, so a diagnostic last_error set for the track that just ended (e.g. the
        # stream getting cut short) is still readable here — grab it now, before that scheduled
        # next_track() -> _start_current() clears last_error for the track it's advancing to.
        if self._player.last_error:
            s.status_message = self._player.last_error
        elif self._player.notice:
            s.status_message = self._player.notice
            self._player.notice = None
        self._sync_player_state()
        after = (s.position_seconds, s.is_playing, s.now_playing.id if s.now_playing else None)
        # Also repaint on nothing-changed ticks whenever something's still loading, so its
        # spinner actually animates instead of sitting on whatever frame it last happened to
        # render (position/is_playing/now_playing don't change during a plain data fetch).
        still_fetching_details = s.now_playing is not None and (s.lyrics is None or s.cover_art is None)
        # Wallpaper visuals are time-driven (see WallpaperPanel) and meant to keep animating even
        # when playback itself hasn't changed (paused, or nothing loaded yet) — repaint every
        # tick while it's active rather than only on an actual state change.
        if before != after or s.is_loading_library or still_fetching_details or s.is_wallpaper_mode or s.is_buffering:
            self.refresh_all()

    def _sync_player_state(self) -> None:
        s = self.state
        previous_id = s.now_playing.id if s.now_playing else None

        s.now_playing = self._player.current
        s.is_playing = self._player.is_playing
        s.is_buffering = self._player.is_buffering
        s.position_seconds = self._player.position_seconds
        s.duration_seconds = self._player.duration_seconds
        s.queue = self._player.queue
        # The queue can change out from under the queue view's own selection (auto-advance,
        # radio auto-extend, a remove shifting indices) — reclamp here rather than only where
        # the view itself edits it, so a stale index never renders as if nothing were selected.
        s.queue_selected_index = max(0, min(s.queue_selected_index, len(s.queue) - 1)) if s.queue else 0
        s.visualizer_levels = self._player.visualizer_levels

        new_id = s.now_playing.id if s.now_playing else None
        if new_id != previous_id:
            self._trigger_lyrics_fetch(s.now_playing)
            self._trigger_cover_art_fetch(s.now_playing)

    def _trigger_lyrics_fetch(self, track: Optional[Track]) -> None:
        self.state.lyrics = None
        if track is None:
            return

        self._lyrics_request_id += 1
        request_id = self._lyrics_request_id

        async def fetch() -> None:
            lyrics = await self._lyrics.get_lyrics(track.title, track.artist, track.duration)
            if request_id == self._lyrics_request_id:
                self.state.lyrics = lyrics
                self.refresh_all()

        asyncio.ensure_future(fetch())

    def _trigger_cover_art_fetch(self, track: Optional[Track]) -> None:
        self.state.cover_art = None
        if track is None or not track.thumbnail_url:
            return

        self._cover_art_request_id += 1
        request_id = self._cover_art_request_id

        async def fetch() -> None:
            art = await self._cover_art.fetch(track.thumbnail_url)
            if request_id != self._cover_art_request_id:
                return

            self.state.cover_art = art
            self.refresh_all()

            if art is not None:
                theme = await asyncio.to_thread(art_theme.build_theme, art)
                if theme is not None and request_id == self._cover_art_request_id:
                    self._apply_art_theme(theme)

        asyncio.ensure_future(fetch())

    def _apply_art_theme(self, theme: Theme) -> None:
        self.register_theme(theme)
        if self.theme == theme.name:
            # Re-registering under the same name doesn't retrigger the reactive watcher (the
            # name string didn't change) — force the CSS variables to recompute so a new track's
            # colors actually take effect instead of only the very first track's.
            self.refresh_css(animate=False)
        else:
            self.theme = theme.name

    # ---- key handling -----------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        s = self.state

        if s.is_searching:
            # The Input widget owns typing/backspace/enter; we only need to intercept Escape.
            if event.key == "escape":
                await self._cancel_search()
                event.stop()
            return

        if event.key == "q":
            self.exit()
            return

        if event.key == "escape":
            if s.viewing_playlist is not None:
                self._close_playlist_view()
                s.status_message = "Back to playlists"
                self.refresh_all()
                return
            if s.is_showing_search_results or s.is_showing_playlist_search_results:
                s.is_showing_search_results = False
                s.is_showing_playlist_search_results = False
                s.tracks_selected_index = 0
                s.playlists_selected_index = 0
                s.status_message = "Back to library"
                self.refresh_all()
            return

        if event.key == "slash":
            # Search targets the *list* of playlists, not one playlist's tracks — back out of
            # browsing first so the results land somewhere that's actually visible.
            if s.left_focus == LeftFocus.PLAYLISTS and s.viewing_playlist is not None:
                self._close_playlist_view()
            s.is_searching = True
            # Search whichever list is focused — Queue counts as Tracks, since there's no such
            # thing as "searching the queue".
            s.search_target = LeftFocus.PLAYLISTS if s.left_focus == LeftFocus.PLAYLISTS else LeftFocus.TRACKS
            s.search_query = ""
            s.status_message = "Search — type and press Enter"
            search_input = self.query_one("#search-input", Input)
            search_input.value = ""
            search_input.add_class("visible")
            search_input.can_focus = True
            search_input.focus()
            self.refresh_all()
            event.stop()
            return

        if event.key == "tab":
            self._move_left_focus(1)
            self.refresh_all()
            return

        if event.key == "v":
            s.is_sidebar_paged = not s.is_sidebar_paged
            self._apply_layout()
            self.refresh_all()
            return

        if event.key == "w":
            s.is_wallpaper_mode = not s.is_wallpaper_mode
            self.screen.set_class(s.is_wallpaper_mode, "wallpaper")
            self.refresh_all()
            return

        if event.key in ("left", "right") and s.is_wallpaper_mode:
            self._cycle_wallpaper_visual(1 if event.key == "right" else -1)
            self.refresh_all()
            return

        if event.key in ("left", "right") and s.is_sidebar_paged:
            self._move_left_focus(1 if event.key == "right" else -1)
            self.refresh_all()
            return

        if event.key == "f":
            s.full_screen_mode = {
                FullScreenMode.NONE: FullScreenMode.COVER_BAR,
                FullScreenMode.COVER_BAR: FullScreenMode.LYRICS,
                FullScreenMode.LYRICS: FullScreenMode.NONE,
            }[s.full_screen_mode]
            self._apply_layout()
            self.refresh_all()
            return

        if event.key == "c":
            s.is_sidebar_collapsed = not s.is_sidebar_collapsed
            self._apply_layout()
            self.refresh_all()
            return

        if event.key == "m":
            s.is_minimal = not s.is_minimal
            self.screen.set_class(s.is_minimal, "minimal")
            self.refresh_all()
            return

        if event.key in ("j", "down"):
            self._move_selection(1)
            self.refresh_all()
            return

        if event.key in ("k", "up"):
            self._move_selection(-1)
            self.refresh_all()
            return

        if event.key == "space":
            await self._player.toggle_pause()
            self._sync_player_state()
            s.status_message = self._player.last_error or ("Resumed" if s.is_playing else "Paused")
            self.refresh_all()
            return

        if event.key == "n":
            self._launch(self._next_track(), loading="Loading next track…")
            return

        if event.key == "p":
            self._launch(self._previous_track(), loading="Loading previous track…")
            return

        if event.key == "enter":
            if s.left_focus == LeftFocus.PLAYLISTS and s.viewing_playlist is None:
                self._launch(self._open_playlist(), loading="Loading playlist…")
            else:
                self._launch(self._play_selection(), loading="Loading…")
            return

        if event.key == "l":
            self._launch(self._toggle_like(), loading="Updating rating…")
            return

        if event.key == "x":
            if s.left_focus == LeftFocus.QUEUE and s.queue:
                self._launch(self._remove_queue_selection(), loading="Removing…")
            return

        if event.key == "o":
            self._auth_service.sign_out()
            self.exit(message="Signed out. You'll need to sign in again next time you start laulja.")
            return

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return

        s = self.state
        s.is_searching = False
        s.search_query = event.value
        event.input.remove_class("visible")
        event.input.can_focus = False
        self.set_focus(None)

        if not s.search_query.strip():
            s.status_message = "Empty query"
            s.is_showing_search_results = False
            s.is_showing_playlist_search_results = False
        elif s.search_target == LeftFocus.PLAYLISTS:
            s.is_showing_playlist_search_results = True
            try:
                playlists = await self._music.search_playlists(s.search_query)
                s.playlist_search_results = playlists
                s.playlists_selected_index = 0
                s.left_focus = LeftFocus.PLAYLISTS
                s.status_message = f"{len(playlists)} playlist(s)"
            except Exception as ex:
                s.playlist_search_results = []
                s.status_message = f"Search failed: {ex}"
        else:
            s.is_showing_search_results = True
            try:
                results = await self._music.search(s.search_query)
                s.search_results = results.tracks
                s.tracks_selected_index = 0
                s.left_focus = LeftFocus.TRACKS
                s.status_message = f"{len(results.tracks)} track(s)"
            except Exception as ex:
                s.search_results = []
                s.status_message = f"Search failed: {ex}"

        self.refresh_all()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self.state.search_query = event.value

    async def _cancel_search(self) -> None:
        s = self.state
        s.is_searching = False
        s.status_message = "Search cancelled"
        search_input = self.query_one("#search-input", Input)
        search_input.remove_class("visible")
        search_input.can_focus = False
        self.set_focus(None)
        self.refresh_all()

    def _move_left_focus(self, delta: int) -> None:
        s = self.state
        i = _LEFT_FOCUS_CYCLE.index(s.left_focus)
        s.left_focus = _LEFT_FOCUS_CYCLE[(i + delta) % len(_LEFT_FOCUS_CYCLE)]
        self._apply_layout()

    def _cycle_wallpaper_visual(self, delta: int) -> None:
        s = self.state
        i = _WALLPAPER_VISUALS.index(s.wallpaper_visual)
        s.wallpaper_visual = _WALLPAPER_VISUALS[(i + delta) % len(_WALLPAPER_VISUALS)]

    def _move_selection(self, delta: int) -> None:
        s = self.state
        if s.left_focus == LeftFocus.TRACKS:
            count = len(s.displayed_tracks)
            if count == 0:
                return
            s.tracks_selected_index = max(0, min(s.tracks_selected_index + delta, count - 1))
        elif s.left_focus == LeftFocus.PLAYLISTS:
            if s.viewing_playlist is not None:
                count = len(s.playlist_view_tracks)
                if count == 0:
                    return
                s.playlist_view_selected_index = max(0, min(s.playlist_view_selected_index + delta, count - 1))
                return
            count = len(s.displayed_playlists)
            if count == 0:
                return
            s.playlists_selected_index = max(0, min(s.playlists_selected_index + delta, count - 1))
        else:
            count = len(s.queue)
            if count == 0:
                return
            s.queue_selected_index = max(0, min(s.queue_selected_index + delta, count - 1))

    def _launch(self, coro, *, loading: str) -> None:
        """Runs `coro` in the background and repaints once it's done, instead of awaiting it
        directly in on_key. Resolving a stream URL + starting ffmpeg (play/next/previous) can
        take a couple of seconds; on_key is awaited to completion by Textual's own key dispatch
        before it'll process the *next* key event, so awaiting that chain in-place froze the
        whole UI — cursor movement included — until it finished. `loading` is shown immediately
        so pressing the key still gives instant feedback despite the real work happening after
        on_key has already returned."""
        s = self.state
        s.status_message = loading
        self.refresh_all()

        async def run() -> None:
            await coro
            self.refresh_all()

        asyncio.ensure_future(run())

    async def _open_playlist(self) -> None:
        """Enter on a playlist opens it for browsing — like clicking into a playlist's page in
        YT Music itself — rather than immediately queuing + playing it; playback only starts
        once you actually pick a track inside it (see _play_selection's PLAYLISTS branch)."""
        s = self.state
        if not s.displayed_playlists:
            s.status_message = "No playlists"
            return

        playlist = s.displayed_playlists[s.playlists_selected_index]
        try:
            tracks = await self._music.get_playlist_tracks(playlist.id)
        except Exception as ex:
            s.status_message = f"Couldn't open playlist: {ex}"
            return

        s.viewing_playlist = playlist
        s.playlist_view_tracks = tracks
        s.playlist_view_selected_index = 0
        s.status_message = f"{len(tracks)} track(s)" if tracks else "Playlist is empty"

    def _close_playlist_view(self) -> None:
        s = self.state
        s.viewing_playlist = None
        s.playlist_view_tracks = []
        s.playlist_view_selected_index = 0

    async def _play_selection(self) -> None:
        s = self.state
        if s.left_focus == LeftFocus.PLAYLISTS:
            # Only reached once a playlist is already open (see on_key's "enter" dispatch) —
            # picking a track here plays the *whole* playlist in order, starting at that track,
            # same as playing a playlist has always queued it (just entered a different way now).
            if not s.playlist_view_tracks:
                s.status_message = "Playlist has no tracks"
                return

            await self._player.play_queue(s.playlist_view_tracks, s.playlist_view_selected_index)
        elif s.left_focus == LeftFocus.QUEUE:
            if not s.queue:
                s.status_message = "Queue is empty"
                return

            await self._player.play_at(s.queue_selected_index)
        else:
            tracks = s.displayed_tracks
            if not tracks:
                s.status_message = "Nothing to play"
                return

            # Start a radio/mix seeded from the picked track rather than just queuing the
            # library/search list as-is — matches YouTube Music's own behavior of playing
            # similar-vibe songs after whatever you selected.
            await self._player.play_track_radio(tracks[s.tracks_selected_index])

        self._sync_player_state()
        s.status_message = self._player.last_error or (
            f"Playing {s.now_playing.title}" if s.now_playing else "Playing"
        )

    async def _next_track(self) -> None:
        await self._player.next_track()
        self._sync_player_state()
        self.state.status_message = self._player.last_error or "Next track"

    async def _previous_track(self) -> None:
        if self._player.queue and self._player.index <= 0:
            self.state.status_message = "Already at the first track"
            return

        await self._player.previous_track()
        self._sync_player_state()
        self.state.status_message = self._player.last_error or "Previous track"

    async def _remove_queue_selection(self) -> None:
        s = self.state
        index = s.queue_selected_index
        error = await self._player.remove_at(index)
        self._sync_player_state()
        if error:
            s.status_message = error
            return

        # Keep the selection sane after the list shrank by one.
        s.queue_selected_index = max(0, min(index, len(s.queue) - 1))
        s.status_message = "Removed from queue"

    async def _toggle_like(self) -> None:
        s = self.state
        track = s.now_playing
        if track is None:
            s.status_message = "Nothing playing to like"
            return

        now_liked = track.id not in s.liked_track_ids
        try:
            await self._music.rate_song(track.id, now_liked)
        except Exception as ex:
            s.status_message = f"Couldn't update rating: {ex}"
            return

        if now_liked:
            s.liked_track_ids.add(track.id)
            s.status_message = f"Liked {track.title}"
        else:
            s.liked_track_ids.discard(track.id)
            s.status_message = f"Unliked {track.title}"

    # ---- layout ------------------------------------------------------------

    def _apply_layout(self) -> None:
        s = self.state
        left = self.query_one("#left")
        center = self.query_one("#center")
        lyrics = self.query_one("#lyrics")

        if s.full_screen_mode == FullScreenMode.LYRICS:
            left.add_class("hidden")
            center.add_class("hidden")
            lyrics.remove_class("hidden")
            lyrics.add_class("fill")
        elif s.full_screen_mode == FullScreenMode.COVER_BAR or s.is_sidebar_collapsed:
            left.add_class("hidden")
            center.remove_class("hidden")
            lyrics.add_class("hidden")
            lyrics.remove_class("fill")
        else:
            left.remove_class("hidden")
            center.remove_class("hidden")
            lyrics.remove_class("hidden")
            lyrics.remove_class("fill")

        panels = {
            LeftFocus.TRACKS: self.query_one("#tracks"),
            LeftFocus.QUEUE: self.query_one("#queue"),
            LeftFocus.PLAYLISTS: self.query_one("#playlists"),
        }
        for focus, panel in panels.items():
            if s.is_sidebar_paged and focus != s.left_focus:
                panel.add_class("hidden")
                panel.remove_class("fill")
            else:
                panel.remove_class("hidden")
                panel.set_class(s.is_sidebar_paged, "fill")

    # ---- rendering -----------------------------------------------------------

    def refresh_all(self) -> None:
        current_theme = self.get_theme(self.theme)
        accent = current_theme.accent if current_theme else None

        self.query_one(HeaderBar).refresh_content()
        self.query_one(TracksPanel).refresh_content()
        self.query_one(PlaylistsPanel).refresh_content()
        self.query_one(QueuePanel).refresh_content()
        self.query_one(CoverArtPanel).refresh_content()
        self.query_one(BarVisualizerPanel).refresh_content(color=accent)
        self.query_one(LyricsPanel).refresh_content(accent=accent)
        self.query_one(QuickActionsBar).refresh_content()
        self.query_one(PlayerInfoBar).refresh_content(accent=accent)
        self.query_one("#player-gauge", ProgressBar).update(progress=self.state.progress_ratio * 1000)

        # Skipped when inactive — plasma/spectrum in particular do real per-frame work that'd
        # otherwise run at the full tick rate for a panel nobody can see.
        if self.state.is_wallpaper_mode:
            self.query_one(WallpaperPanel).refresh_content(accent=accent)
