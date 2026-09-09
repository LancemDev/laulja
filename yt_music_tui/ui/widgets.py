from __future__ import annotations

import io
import os
import textwrap
import time
from datetime import timedelta
from typing import Optional

import pyfiglet
from PIL import Image as PILImage
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static
from textual_image.widget import Image as _AutoCoverImage
from textual_image.widget import SixelImage as _SixelCoverImage

# textual-image auto-detects Sixel/Kitty-graphics-protocol support by writing an escape-code
# query and giving the terminal 100ms to answer (its own docstring calls this "a bit flaky —
# keystrokes during reading the response can lead to false answers"). Konsole in particular
# has supported Sixel since v22.04, but its response to that query can still just not land in
# time, silently downgrading it to blurry half-block-character rendering. Since Konsole sets
# $KONSOLE_VERSION, we can skip the flaky round-trip for it entirely and force real Sixel.
CoverImage = _SixelCoverImage if os.environ.get("KONSOLE_VERSION") else _AutoCoverImage

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


# Biggest/blockiest first, each a fallback for the one before. Both are plain-ASCII figlet
# fonts (only space/_/|/\//.` etc.) on purpose — not the +-| per-letter boxes of "digital"
# (illegible/ugly) and not any Unicode block-drawing font (solid "█" full blocks included):
# those depend on the terminal's font tiling that glyph pixel-perfectly, and on terminals
# that don't, they render as a broken checkerboard/outline instead of a solid letter. Plain
# ASCII always renders identically everywhere. "big"'s letters are still ~7 columns wide,
# too wide for every word of a line to fit in a narrow lyrics panel — pyfiglet wraps a
# too-wide *line* at word boundaries fine, but tears a too-wide single *word* across rows
# mid-letter instead, which is unreadable — so "small" (narrower) is the fallback for that.
_BIG_TEXT_FONTS = ["big", "small"]


def _figlet_word_width(word: str, font: str) -> int:
    """How many columns `word` alone renders to in `font` — used to check a word will fit on
    one row before committing to a font, since pyfiglet itself will silently tear a too-wide
    word across rows mid-letter rather than refuse to render it."""
    try:
        rendered = pyfiglet.Figlet(font=font, width=10_000).renderText(word)
    except Exception:
        return len(word)
    return max((len(row.rstrip()) for row in rendered.split("\n")), default=len(word))


def _big_text_rows(text: str, width: int) -> list[str]:
    """Renders `text` as big boxed-letter art, word-wrapped to `width` columns — the closest a
    terminal can get to the reference video's large caption text, since actual font-size scaling
    isn't something a terminal can do. Falls back to a narrower font, and finally to plain text,
    rather than let a big font's letters be wider than the panel and force pyfiglet to split a
    word mid-letter to make it "fit"."""
    width = max(10, width)
    words = text.split()

    for font in _BIG_TEXT_FONTS:
        if words and max(_figlet_word_width(w, font) for w in words) > width:
            continue
        try:
            rendered = pyfiglet.Figlet(font=font, width=width).renderText(text)
        except Exception:
            continue
        rows = [row.rstrip() for row in rendered.split("\n")]
        while rows and not rows[0]:
            rows.pop(0)
        while rows and not rows[-1]:
            rows.pop()
        if rows:
            return rows

    # Not even the narrowest big font's letters fit every word in this line — give up on
    # block-letter art for it and just wrap plain text, which stays legible at any width.
    return textwrap.wrap(text, width) or [text]


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
        # Unlike Tracks/Playlists, lyrics content should wrap: the plain (unsynced) lyrics dump
        # needs full lines readable rather than truncated, and the big block-letter rows are
        # already word-wrapped to the panel width by _big_text_rows.
        super().__init__(wrap=True, **kwargs)
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


def _cover_crop_box(source_size: tuple[int, int], target_ratio: float) -> tuple[int, int, int, int]:
    """The centered crop box on `source_size` (width, height) matching `target_ratio`
    (width/height) — the crop half of CSS `object-fit: cover`, deliberately without the resize
    half, so the result stays at the source's own resolution."""
    src_w, src_h = source_size
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w, new_h = max(1, round(src_h * target_ratio)), src_h
    else:
        new_w, new_h = src_w, max(1, round(src_w / target_ratio))
    left, top = (src_w - new_w) // 2, (src_h - new_h) // 2
    return (left, top, left + new_w, top + new_h)


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
        #
        # Crop only — do NOT also resize down to (cols, rows). Those are terminal *cells*, not
        # pixels (a cell is many pixels), and textual-image's renderers (sixel.py/tgp.py) already
        # scale the image we hand them to the terminal's real pixel dimensions themselves, via
        # get_cell_size(). Pre-shrinking to a cell-count-sized bitmap here fed a tiny image into
        # that scaling step, which then stretched it back up — exactly what produced the blur.
        key = (*source_key, cols, rows)
        if key != self._shown_key:
            target_ratio = cols / max(1, round(rows / self._CELL_ASPECT))
            fitted = self._source_image.crop(_cover_crop_box(self._source_image.size, target_ratio))

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
