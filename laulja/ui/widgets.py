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


def _pick_big_font(words: list[str], width: int) -> Optional[str]:
    """Chooses one figlet font that fits every word in `words` at `width` columns. Picking this
    once for the whole song (from every line's words) rather than per rendered line keeps the
    block-letter art visually consistent instead of flipping fonts line to line whenever one
    line happens to contain a longer word than its neighbours."""
    for font in _BIG_TEXT_FONTS:
        if not words or max(_figlet_word_width(w, font) for w in words) <= width:
            return font
    return None


def _big_text_rows(text: str, width: int, font: Optional[str]) -> list[str]:
    """Renders `text` as big boxed-letter art in `font`, word-wrapped to `width` columns — the
    closest a terminal can get to the reference video's large caption text, since actual
    font-size scaling isn't something a terminal can do. Falls back to plain text if `font` is
    None (nothing in _BIG_TEXT_FONTS fits every word of the song) or fails to render."""
    width = max(10, width)
    if font is not None:
        try:
            rendered = pyfiglet.Figlet(font=font, width=width).renderText(text)
            rows = [row.rstrip() for row in rendered.split("\n")]
            while rows and not rows[0]:
                rows.pop(0)
            while rows and not rows[-1]:
                rows.pop()
            if rows:
                return rows
        except Exception:
            pass

    # Not even the narrowest big font's letters fit every word of this song — give up on
    # block-letter art and just wrap plain text, which stays legible at any width.
    return textwrap.wrap(text, width) or [text]


def _format_short_duration(d: Optional[timedelta]) -> str:
    if d is None:
        return "--:--"
    total = int(d.total_seconds())
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _format_seconds(total_seconds: float) -> str:
    total = max(0, int(total_seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


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
            if s.is_searching and s.search_target == LeftFocus.TRACKS
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
        title = (
            f"Search: /{s.search_query}█"
            if s.is_searching and s.search_target == LeftFocus.PLAYLISTS
            else f"Playlists · Search: {s.search_query}"
            if s.is_showing_playlist_search_results
            else "Playlists"
        )
        if s.left_focus == LeftFocus.PLAYLISTS:
            title = f"▶ {title}"
        self.border_title = title

        playlists = s.displayed_playlists
        if not playlists:
            if s.is_loading_library and not s.is_showing_playlist_search_results:
                self.set_lines(f"{_spinner()} Loading your playlists…", None, 0)
            else:
                self.set_lines("No results." if s.is_showing_playlist_search_results else "No playlists yet.", None, 0)
            return

        text = Text()
        for i, p in enumerate(playlists):
            line = f"{p.title}  ({p.track_count})"
            style = "reverse" if i == s.playlists_selected_index else ""
            if i:
                text.append("\n")
            text.append(line, style=style)

        self.set_lines(text, s.playlists_selected_index, len(playlists))


class QueuePanel(ListPanel):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        title = "Queue"
        if s.left_focus == LeftFocus.QUEUE:
            title = f"▶ {title}"
        self.border_title = title

        queue = s.queue
        if not queue:
            self.set_lines("Queue is empty.", None, 0)
            return

        now_playing_id = s.now_playing.id if s.now_playing else None
        text = Text()
        for i, t in enumerate(queue):
            marker = "♪ " if t.id == now_playing_id else "  "
            line = f"{marker}{t.title}  ·  {t.artist}  [{_format_short_duration(t.duration)}]"
            style = "reverse" if i == s.queue_selected_index else ""
            if i:
                text.append("\n")
            text.append(line, style=style)

        self.set_lines(text, s.queue_selected_index, len(queue))


class LyricsPanel(ListPanel):
    def __init__(self, state: AppState, **kwargs) -> None:
        # Unlike Tracks/Playlists, lyrics content should wrap: the plain (unsynced) lyrics dump
        # needs full lines readable rather than truncated, and the big block-letter rows are
        # already word-wrapped to the panel width by _big_text_rows.
        super().__init__(wrap=True, **kwargs)
        self.state = state
        self._rendered_key: Optional[tuple] = None
        self._font_key: Optional[tuple] = None
        self._big_font: Optional[str] = None

    def refresh_content(self, accent: Optional[str] = None) -> None:
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

        # Only the active line (or panel size, or the art-derived accent color) changing should
        # trigger a repaint + scroll — re-triggering the scroll animation every tick with the
        # same target would keep restarting it mid-flight instead of ever settling.
        key = (id(lyrics), active, cols, height, accent)
        if key == self._rendered_key:
            return
        self._rendered_key = key

        # Pick the block-letter font once per song (per panel width) from every line's words,
        # rather than per rendered line — so the active line never jumps fonts as playback
        # moves from a line with short words to one with a longer word.
        font_key = (id(lyrics), cols)
        if font_key != self._font_key:
            words = [w for line in lyrics.lines for w in (line.text or "").upper().split()]
            self._big_font = _pick_big_font(words, cols)
            self._font_key = font_key

        # Only the line being sung right now — not the lines around it — but rendered as big
        # block-letter text (word-wrapped to the panel width) instead of plain bold color, since
        # a terminal can't scale font size the way the reference video's caption does.
        line_text = (lyrics.lines[active].text or " ").upper()
        block_rows = _big_text_rows(line_text, cols, self._big_font)
        pad = max(0, (height - len(block_rows)) // 2)

        # Colored with the current art-derived theme's accent (art_theme.py), same as the
        # now-playing bar's fill — falls back to a fixed color only until the first cover art
        # loads and a theme accent actually exists.
        line_style = f"bold {accent}" if accent else "bold cyan1"

        text = Text(justify="center")
        if pad:
            text.append("\n" * pad)
        for i, row in enumerate(block_rows):
            if i:
                text.append("\n")
            text.append(row, style=line_style)
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


def _cover_fit_size(source_size: tuple[int, int], max_cols: int, max_rows: int, cell_aspect: float) -> tuple[int, int]:
    """The largest (cols, rows) cell box within (max_cols, max_rows) that keeps `source_size`'s
    (width, height) pixel aspect ratio — CSS `object-fit: contain`, letterboxed instead of
    cropped, so the whole image is always visible at its own proportions and just grows/shrinks
    with the panel rather than having its edges cut to always fill it. `cell_aspect` (cell
    width/height) converts between cell counts and the pixel-equivalent ratio math, same as the
    fixed assumption used elsewhere in this class instead of a live (flaky) terminal query."""
    src_w, src_h = source_size
    src_ratio = src_w / src_h
    max_cols, max_rows = max(1, max_cols), max(1, max_rows)
    target_ratio = max_cols / max(1, round(max_rows / cell_aspect))

    if src_ratio > target_ratio:
        # Image is proportionately wider than the box — width is the constraint, letterbox
        # top/bottom.
        cols = max_cols
        rows = max(1, round(cols / src_ratio * cell_aspect))
    else:
        # Image is proportionately taller (or equal) — height is the constraint, pillarbox
        # left/right.
        rows = max_rows
        cols = max(1, round(src_ratio * (rows / cell_aspect)))
    return cols, rows


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

        # Size the widget itself to the largest box that fits within the panel without
        # distorting the image's own aspect ratio (like CSS `object-fit: contain`), rather than
        # always filling the panel and cropping/stretching to do it — so resizing the window
        # shrinks or grows the art at its own proportions instead of changing what's visible.
        # `align: center middle` on the parent then letterboxes/pillarboxes it. The source
        # bitmap itself is handed over uncropped, at its own resolution — textual-image's
        # renderers (sixel.py/tgp.py) scale it to the terminal's real pixel dimensions for
        # whatever cell box we give the widget, via get_cell_size().
        key = (*source_key, cols, rows)
        if key != self._shown_key:
            fit_cols, fit_rows = _cover_fit_size(self._source_image.size, cols, rows, self._CELL_ASPECT)
            image_widget.styles.width = fit_cols
            image_widget.styles.height = fit_rows

            image_widget.image = self._source_image
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


class PlayerInfoBar(Static):
    """The "now playing" bar, doubling as a progress indicator: a fill sweeps across it
    left-to-right as the song plays — empty at 0:00, fully filled right as the track ends —
    colored with the current art-derived theme's accent (art_theme.py) rather than a fixed
    color, so it's actually the album/theme color.

    Padded to this widget's own width and filled via a Rich Text background span rather than an
    overlaid second widget: Textual's `layer:` CSS only reorders *non-overlapping* siblings
    (floats/docked widgets, tooltips, etc.) — two plain same-container children on different
    layers still don't actually paint over one another, confirmed empirically, so a real
    "layered fill" widget silently renders as if the fill weren't there at all.

    Uses `self.size.width` (this widget's real, post-layout outer size) minus its own known
    border+padding, not `self.content_size` — that property doesn't reliably net out this
    widget's border/padding (it was observed equal to the outer size, border included), so the
    fill visibly fell short of the box's true right edge when computed from it directly."""

    _BORDER_AND_PADDING_COLS = 4  # round border (1+1) + "padding: 0 1" (1+1)

    DEFAULT_CSS = """
    PlayerInfoBar {
        height: 3;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self, accent: Optional[str] = None) -> None:
        s = self.state
        icon = "▶" if s.is_playing else "⏸"
        if s.now_playing:
            liked = " ♥" if s.now_playing.id in s.liked_track_ids else ""
            line = f"{icon}  {s.now_playing.title} — {s.now_playing.artist}{liked}"
        else:
            line = "Nothing playing  ·  Enter play · Space pause · n/p skip"
        pos = _format_seconds(s.position_seconds)
        dur = _format_seconds(s.duration_seconds)
        text = f"{line}  [{pos} / {dur}]"

        fill_style = f"black on {accent}" if accent else "reverse"
        content_width = max(0, int(self.size.width) - self._BORDER_AND_PADDING_COLS)
        width = max(len(text), content_width)
        padded = text.ljust(width)
        filled = int(max(0.0, min(1.0, s.progress_ratio)) * width)

        display = Text()
        if filled:
            display.append(padded[:filled], style=fill_style)
        display.append(padded[filled:])
        self.update(display)


_KEY_ACTIONS = [
    ("Tab", "focus"),
    ("j/k", "move"),
    ("Enter", "play"),
    ("Space", "pause"),
    ("n/p", "next/prev"),
    ("l", "like"),
    ("/", "search"),
    ("f", "fullscreen"),
    ("c", "collapse"),
    ("m", "minimal"),
    ("q", "quit"),
]

# "x remove" only does anything with the Queue panel focused, so it's spliced into the hint bar
# there instead of always showing — matches _KEY_ACTIONS' own position for where it'd otherwise
# sit, right after next/prev.
_QUEUE_REMOVE_ACTION = ("x", "remove")


class QuickActionsBar(Static):
    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state

    def refresh_content(self) -> None:
        # Keys get a boxed "keycap" look so they read as literal keys to press, distinct from
        # the plain-text action next to them — a flat "Tab focus" run-on reads ambiguous to
        # someone who doesn't already know the bindings.
        actions = list(_KEY_ACTIONS)
        if self.state.left_focus == LeftFocus.QUEUE:
            actions.insert(5, _QUEUE_REMOVE_ACTION)

        divider = "   "
        pairs = (f"[b reverse] {key} [/]  {action}" for key, action in actions)
        self.update(divider.join(pairs))
