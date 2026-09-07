from __future__ import annotations

import io
import time
from datetime import timedelta
from typing import Optional

import pyfiglet
from PIL import Image as PILImage, ImageOps
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static
from textual_image.widget import Image as CoverImage

from .listing import ListPanel
from .state import AppState, LeftFocus

"""Panel widgets — the Python/Textual equivalents of UI/Widgets/*.cs. Each widget holds a
reference to the shared AppState and repaints itself on demand via refresh_content(),
mirroring the original's Draw(term, area, state) static methods."""


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner() -> str:
    """A Braille-dot spinner frame driven off wall-clock time — no state to thread through
    AppState, it just animates on its own each time a loading panel gets repainted."""
    return _SPINNER_FRAMES[int(time.monotonic() * 10) % len(_SPINNER_FRAMES)]


_BIG_TEXT_FONT = "double_blocky"  # solid block glyphs, only 2 rows tall — a terminal row is
# about twice as tall as a column, so a 4-row font (smblock) rendered letters looking stretched
# tall; this stays legible and bold without the elongation.

_SHADE_TO_SOLID = str.maketrans("░▒▓", "███")  # see _big_text_rows


def _big_text_rows(text: str, width: int) -> list[str]:
    """Renders `text` as big block-letter art, word-wrapped to `width` columns — the closest a
    terminal can get to the reference video's large caption text, since actual font-size scaling
    isn't something a terminal can do."""
    try:
        rendered = pyfiglet.Figlet(font=_BIG_TEXT_FONT, width=max(10, width)).renderText(text)
    except Exception:
        return [text]

    # double_blocky anti-aliases curved strokes with the ░ shade character, which under a solid
    # color reads as a distracting dotted/perforated texture rather than a clean edge. Solidify
    # it into a full block instead — heavier strokes, but legible rather than textured.
    rendered = rendered.translate(_SHADE_TO_SOLID)

    rows = [row.rstrip() for row in rendered.split("\n")]
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return rows or [text]


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
        self.update(f"[bold]{self.app_name}[/]")


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
            if s.is_loading_library and not s.is_showing_search_results:
                self.set_lines(f"{_spinner()} Loading your library…", None, 0)
            else:
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
            if s.is_loading_library:
                self.set_lines(f"{_spinner()} Loading your playlists…", None, 0)
            else:
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
        self._rendered_key: Optional[tuple] = None

    def refresh_content(self) -> None:
        self.border_title = "Lyrics"
        s = self.state
        lyrics = s.lyrics

        if lyrics is None or not lyrics.lines:
            self._rendered_key = None
            text = f"{_spinner()} Loading lyrics…" if lyrics is None else "No lyrics."
            self.set_lines(text, None, 0)
            return

        if not lyrics.is_synced:
            key = (id(lyrics), None)
            if key != self._rendered_key:
                # No per-line timing to step through — show the plain lyrics block as-is.
                text = Text("\n".join(line.text for line in lyrics.lines))
                self.set_lines(text, None, len(lyrics.lines))
                self._rendered_key = key
            return

        current_index = self._current_line_index(lyrics)
        active = current_index if current_index is not None else 0
        cols = max(1, int(self.size.width))
        height = max(1, int(self.size.height))

        # Only the active line (or panel size) changing should trigger a repaint + scroll —
        # re-triggering the scroll animation every tick with the same target would keep
        # restarting it mid-flight instead of ever settling.
        key = (id(lyrics), active, cols, height)
        if key == self._rendered_key:
            return
        self._rendered_key = key

        # Only the line being sung right now — not the lines around it — but rendered as big
        # block-letter text (word-wrapped to the panel width) instead of plain bold color, since
        # a terminal can't scale font size the way the reference video's caption does.
        line_text = (lyrics.lines[active].text or " ").upper()
        block_rows = _big_text_rows(line_text, cols)
        pad = max(0, (height - len(block_rows)) // 2)

        text = Text(justify="center")
        if pad:
            text.append("\n" * pad)
        for i, row in enumerate(block_rows):
            if i:
                text.append("\n")
            text.append(row, style="bold cyan1")
        if pad:
            text.append("\n" * pad)

        center_index = pad + len(block_rows) // 2
        total_rows = pad + len(block_rows) + pad
        self.set_lines(text, center_index, total_rows, center=True, animate=True)

    def _current_line_index(self, lyrics) -> Optional[int]:
        position = timedelta(seconds=self.state.position_seconds)
        index = None
        for i, line in enumerate(lyrics.lines):
            if line.time is not None and line.time <= position:
                index = i
        return index


class CoverArtPanel(Vertical):
    """Shows the track's real thumbnail via textual-image, which picks the best rendering the
    terminal actually supports (Kitty/Sixel graphics for a true bitmap, falling back to colored
    half-cells or plain unicode) — so this needs no image logic of its own, just a status text
    fallback for when there's nothing to show yet."""

    # Terminal font cells are almost universally close to this width:height ratio (it's even
    # the library's own hardcoded fallback). Used to crop art to the panel's true shape instead
    # of querying the terminal for its exact cell pixel size — that query is documented as flaky
    # ("keystrokes during reading the response can lead to false answers") and a bad reading
    # previously showed up as real, visible stretching. This fixed ratio trades perfect accuracy
    # for reliability: worst case a few percent off (imperceptible), never wildly wrong.
    _CELL_ASPECT = 0.5  # cell width / cell height

    DEFAULT_CSS = """
    CoverArtPanel {
        align: center middle;
    }
    CoverArtPanel > #cover-image {
        width: 100%;
        height: 100%;
    }
    CoverArtPanel > .hidden {
        display: none;
    }
    CoverArtPanel > #cover-status {
        content-align: center middle;
        text-align: center;
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self._source_image: Optional[PILImage.Image] = None
        self._source_key: Optional[tuple] = None
        self._shown_key: Optional[tuple] = None

    def compose(self) -> ComposeResult:
        yield CoverImage(id="cover-image", classes="hidden")
        yield Static(id="cover-status")

    def refresh_content(self) -> None:
        track = self.state.now_playing
        art = self.state.cover_art
        image_widget = self.query_one("#cover-image", CoverImage)
        status_widget = self.query_one("#cover-status", Static)

        if track is None or art is None:
            if self._shown_key is not None:
                image_widget.add_class("hidden")
                image_widget.image = None
                self._source_image = None
                self._source_key = None
                self._shown_key = None
            if track is None:
                lines = ["Nothing playing", "Press Enter on a track to start"]
            else:
                lines = [track.title, track.artist]
                if track.album:
                    lines.append(track.album)
                lines.append("")
                lines.append(f"{_spinner()} Loading cover art…" if track.thumbnail_url else "(no cover art)")
            status_widget.remove_class("hidden")
            status_widget.update("\n".join(lines))
            return

        cols, rows = int(self.size.width), int(self.size.height)
        if cols <= 0 or rows <= 0:
            return

        source_key = (track.id, id(art))
        if source_key != self._source_key:
            self._source_image = PILImage.open(io.BytesIO(art)).convert("RGB")
            self._source_key = source_key

        # Crop to the panel's shape (like CSS `object-fit: cover`) so it fills completely at
        # full size, undistorted — using the fixed cell-aspect assumption above, not a live
        # terminal query, for the target ratio.
        key = (*source_key, cols, rows)
        if key != self._shown_key:
            target = (cols, max(1, round(rows / self._CELL_ASPECT)))
            fitted = ImageOps.fit(self._source_image, target, method=PILImage.Resampling.LANCZOS)

            image_widget.image = fitted
            image_widget.remove_class("hidden")
            status_widget.add_class("hidden")
            self._shown_key = key


_BAR_BLOCKS = " ▁▂▃▄▅▆▇█"


class BarVisualizerPanel(Static):
    DEFAULT_CSS = """
    BarVisualizerPanel {
        content-align: center middle;
        text-align: center;
    }
    """

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


_KEY_ACTIONS = [
    ("Tab", "focus"),
    ("j/k", "move"),
    ("Enter", "play"),
    ("Space", "pause"),
    ("n/p", "next/prev"),
    ("/", "search"),
    ("f", "fullscreen"),
    ("c", "collapse"),
    ("m", "minimal"),
    ("q", "quit"),
]


class QuickActionsBar(Static):
    def refresh_content(self) -> None:
        # Keys get a boxed "keycap" look so they read as literal keys to press, distinct from
        # the plain-text action next to them — a flat "Tab focus" run-on reads ambiguous to
        # someone who doesn't already know the bindings.
        divider = "   "
        pairs = (f"[b reverse] {key} [/]  {action}" for key, action in _KEY_ACTIONS)
        self.update(divider.join(pairs))
