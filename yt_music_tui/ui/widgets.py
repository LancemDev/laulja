from __future__ import annotations

from datetime import timedelta
from typing import Optional

from rich.text import Text
from textual.widgets import Static

from .image_render import render_half_blocks
from .listing import ListPanel
from .state import AppState, LeftFocus

"""Panel widgets — the Python/Textual equivalents of UI/Widgets/*.cs. Each widget holds a
reference to the shared AppState and repaints itself on demand via refresh_content(),
mirroring the original's Draw(term, area, state) static methods."""


def _format_short_duration(d: Optional[timedelta]) -> str:
    if d is None:
        return "--:--"
    total = int(d.total_seconds())
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


class HeaderBar(Static):
    def __init__(self, state: AppState, app_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.app_name = app_name

    def refresh_content(self) -> None:
        self.border_title = self.app_name
        s = self.state
        self.update(f"[{s.auth_label}]  ·  {s.status_message}")


class TracksPanel(ListPanel):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        title = (
            f"Search: /{s.search_query}█"
            if s.is_searching
            else f"Tracks · Search: {s.search_query}"
            if s.is_showing_search_results
            else "Tracks · Library"
        )
        if s.left_focus == LeftFocus.TRACKS:
            title = f"▶ {title}"
        self.border_title = title

        tracks = s.displayed_tracks
        if not tracks:
            self.set_lines("No results." if s.is_showing_search_results else "No tracks yet.", None, 0)
            return

        text = Text()
        for i, t in enumerate(tracks):
            marker = "♪ " if s.now_playing and s.now_playing.id == t.id else "  "
            line = f"{marker}{t.title}  ·  {t.artist}  [{_format_short_duration(t.duration)}]"
            style = "reverse" if i == s.tracks_selected_index else ""
            if i:
                text.append("\n")
            text.append(line, style=style)

        self.set_lines(text, s.tracks_selected_index, len(tracks))


class PlaylistsPanel(ListPanel):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        title = "Playlists"
        if s.left_focus == LeftFocus.PLAYLISTS:
            title = f"▶ {title}"
        self.border_title = title

        if not s.playlists:
            self.set_lines("No playlists yet.", None, 0)
            return

        text = Text()
        for i, p in enumerate(s.playlists):
            line = f"{p.title}  ({p.track_count})"
            style = "reverse" if i == s.playlists_selected_index else ""
            if i:
                text.append("\n")
            text.append(line, style=style)

        self.set_lines(text, s.playlists_selected_index, len(s.playlists))


class LyricsPanel(ListPanel):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        self.border_title = "Lyrics"
        s = self.state
        lyrics = s.lyrics

        if lyrics is None or not lyrics.lines:
            self.set_lines("Loading lyrics…", None, 0)
            return

        if not lyrics.is_synced:
            # No per-line timing to step through — show the plain lyrics block as-is.
            text = Text("\n".join(line.text for line in lyrics.lines))
            self.set_lines(text, None, len(lyrics.lines))
            return

        current_index = self._current_line_index(lyrics)
        index = current_index if current_index is not None else 0
        self.set_lines(Text(f"▶ {lyrics.lines[index].text}"), None, 1)

    def _current_line_index(self, lyrics) -> Optional[int]:
        position = timedelta(seconds=self.state.position_seconds)
        index = None
        for i, line in enumerate(lyrics.lines):
            if line.time is not None and line.time <= position:
                index = i
        return index


class CoverArtPanel(Static):
    DEFAULT_CSS = """
    CoverArtPanel {
        content-align: center middle;
        text-align: center;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self._render_cache_key: Optional[tuple] = None

    def refresh_content(self) -> None:
        self.border_title = "Now Playing"
        track = self.state.now_playing
        art = self.state.cover_art

        if track is None or art is None:
            self._render_cache_key = None
            if track is None:
                lines = ["Nothing playing", "Press Enter on a track to start"]
            else:
                lines = [track.title, track.artist]
                if track.album:
                    lines.append(track.album)
                lines.append("")
                lines.append("Loading cover art…" if track.thumbnail_url else "(no cover art)")
            self.update("\n".join(lines))
            return

        cols, rows = int(self.size.width), int(self.size.height)
        if cols <= 0 or rows <= 0:
            return

        key = (track.id, cols, rows, id(art))
        if key != self._render_cache_key:
            self.update(render_half_blocks(art, cols, rows))
            self._render_cache_key = key


_BAR_BLOCKS = " ▁▂▃▄▅▆▇█"


class BarVisualizerPanel(Static):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        levels = self.state.visualizer_levels
        if not levels:
            self.update("")
            return

        height = max(1, int(self.size.height) or 4)
        rows = []
        for row in range(height):
            cells = []
            for level in levels:
                ratio = max(0.0, min(1.0, level / 100.0))
                # How full is this row (counted from the bottom) for this bar's height?
                cell_value = ratio * height - (height - row - 1)
                cell_value = max(0.0, min(1.0, cell_value))
                idx = int(round(cell_value * (len(_BAR_BLOCKS) - 1)))
                cells.append(_BAR_BLOCKS[idx])
            rows.append(" ".join(cells))
        self.update("\n".join(rows))


class QuickActionsBar(Static):
    def refresh_content(self) -> None:
        self.update(
            "Tab focus  |  j/k move  |  Enter play  |  Space pause  |  n/p next/prev  |  "
            "/ search  |  f fullscreen  |  c collapse  |  q quit"
        )
