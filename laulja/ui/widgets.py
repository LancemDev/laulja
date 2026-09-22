from __future__ import annotations

import colorsys
import io
import math
import os
import random
import textwrap
import time
from datetime import timedelta
from typing import List, Optional, Tuple

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
from .state import AppState, LeftFocus, WallpaperVisual

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

# How many lyric lines to show above/below the currently-playing one, each shrunk to a single
# plain-text row and progressively dimmed — a terminal-cell stand-in for the "lines recede into
# depth" look (real scale/blur isn't something a character grid can do), capped so it still fits
# comfortably alongside the big current-line block on a normal-height lyrics panel.
_LYRICS_CONTEXT_LINES = 2


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


def _ellipsize(text: str, width: int) -> str:
    """Truncates `text` to `width` columns with a trailing ellipsis instead of letting it wrap —
    context lyric rows are meant to be exactly one row tall each (see LyricsPanel), and the panel
    itself has word-wrap CSS on for the plain-lyrics fallback, so an over-long context line would
    otherwise spill onto an extra row and throw off the fixed row-count layout."""
    text = text or ""
    width = max(1, width)
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1].rstrip() + "…"


def _fade_hex(hex_color: Optional[str], amount: float) -> str:
    """Blends `hex_color` toward neutral grey by `amount` (0 = full color, 1 = grey) — the
    terminal stand-in for the reference effect's opacity/blur falloff, since a cell grid can't
    do real alpha blending. Used to dim lyric lines further from the one currently playing."""
    base = hex_color or "#ffa62b"
    try:
        r, g, b = (int(base[i : i + 2], 16) for i in (1, 3, 5))
    except Exception:
        r, g, b = (0xFF, 0xA6, 0x2B)
    amount = max(0.0, min(1.0, amount))
    r = round(r + (0x80 - r) * amount)
    g = round(g + (0x80 - g) * amount)
    b = round(b + (0x80 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _figlet_group_height(font: Optional[str], width: int) -> int:
    """Row-height of a single pyfiglet word-wrapped group in `font` at `width` columns — a short
    probe word is guaranteed to render as exactly one such group, so this gives the fixed "one
    tier of big text" height regardless of how many groups any particular lyric line's own
    length happens to wrap into."""
    if font is None:
        return 1
    return max(1, len(_big_text_rows("I", width, font)))


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
        self._big_block_height: int = 1

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
        # rather than per rendered line — so the active line never jumps fonts as playback moves
        # from a line with short words to one with a longer word. The reserved block height for
        # the current-line tier is capped at two word-wrapped groups' worth of rows (a fixed
        # function of the font+width, not of any particular line's length): sizing it off the
        # single longest lyric line in the song instead would mean one outlier line — most songs
        # have at least one — reserves a towering slot that every short line then pads out to,
        # which is exactly the "hops around"/inconsistent feel this is meant to fix.
        font_key = (id(lyrics), cols)
        if font_key != self._font_key:
            words = [w for line in lyrics.lines for w in (line.text or "").upper().split()]
            self._big_font = _pick_big_font(words, cols)
            self._big_block_height = _figlet_group_height(self._big_font, cols) * 2 if self._big_font else 3
            self._font_key = font_key

        # Colored with the current art-derived theme's accent (art_theme.py), same as the
        # now-playing bar's fill — falls back to a fixed warm color only if the app's theme
        # lookup itself ever comes back empty (the default theme set at startup normally means
        # an accent is always available well before any cover art loads).
        current_style = f"bold {accent}" if accent else "bold #ffa62b"
        block_height = min(self._big_block_height, height)

        # The line being sung right now, rendered as big block-letter text (word-wrapped to the
        # panel width) inside a fixed-height slot — a terminal can't scale font size the way the
        # reference video's caption does, so size is faked with this one oversized tier. A line
        # too long to fit the reserved slot even wrapped (an outlier next to the rest of the
        # song) falls back to plain bold text for just that line, rather than blowing past the
        # slot and dragging every other line's layout back out of sync with it.
        active_text = lyrics.lines[active].text or " "
        block_rows = _big_text_rows(active_text.upper(), cols, self._big_font)
        if len(block_rows) > block_height:
            block_rows = (textwrap.wrap(active_text, cols) or [active_text])[:block_height]
        block_pad_before = max(0, (block_height - len(block_rows)) // 2)
        block_pad_after = max(0, block_height - len(block_rows) - block_pad_before)

        # Lines around the current one, shown as plain single-row text that fades toward grey the
        # further they are from the current line — the terminal stand-in for the reference
        # effect's shrink/blur/fade-with-distance. None of these are ever bold: bold is reserved
        # for the one current line (see current_style above), so it's unambiguous which single
        # line is actually playing instead of the nearest neighbor reading as highlighted too.
        context_budget = max(0, (height - block_height) // 2)
        context_n = max(0, min(_LYRICS_CONTEXT_LINES, context_budget))
        outer_pad = max(0, (height - block_height - context_n * 2) // 2)

        def context_row(offset: int) -> tuple[str, str]:
            idx = active + offset
            text = lyrics.lines[idx].text if 0 <= idx < len(lyrics.lines) else ""
            fade = 0.35 if abs(offset) == 1 else 0.65
            color = _fade_hex(accent, fade)
            weight = "" if abs(offset) == 1 else "dim"
            return _ellipsize(text, cols), f"{weight} {color}".strip()

        text = Text(justify="center")
        if outer_pad:
            text.append("\n" * outer_pad)
        for offset in range(-context_n, 0):
            row, style = context_row(offset)
            text.append(row, style=style)
            text.append("\n")
        if block_pad_before:
            text.append("\n" * block_pad_before)
        for i, row in enumerate(block_rows):
            if i:
                text.append("\n")
            text.append(row, style=current_style)
        if block_pad_after:
            text.append("\n" * block_pad_after)
        for offset in range(1, context_n + 1):
            row, style = context_row(offset)
            text.append("\n")
            text.append(row, style=style)
        if outer_pad:
            text.append("\n" * outer_pad)

        center_index = outer_pad + context_n + block_pad_before + len(block_rows) // 2
        total_rows = outer_pad + context_n + block_height + context_n + outer_pad
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

    def refresh_content(self, color: Optional[str] = None) -> None:
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
        # Colored with the current art-derived theme's accent (same as the lyrics/now-playing
        # fill) — falls back to a fixed color only until the first cover art loads and a theme
        # accent actually exists.
        self.update(Text("\n".join(rows), style=color or "bold white"))


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


def _accent_hue(accent: Optional[str]) -> float:
    """The accent color's hue (0..1), so a visual's generated palette rotates around whatever
    the current art-derived theme actually is instead of a hardcoded color — falls back to the
    app's own default warm amber (see app.py's _DEFAULT_THEME) before any theme accent exists."""
    if accent:
        try:
            r, g, b = (int(accent[i : i + 2], 16) / 255 for i in (1, 3, 5))
            return colorsys.rgb_to_hsv(r, g, b)[0]
        except Exception:
            pass
    return 0.09


def _hsv_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


# (start_col, end_col, style) — one row's worth of colored spans over an otherwise-plain string.
_RowSpans = List[Tuple[int, int, str]]

_PLASMA_GRID_W = 40
_PLASMA_GRID_H = 18
_RAIN_CHARS = "|:.`"


class WallpaperPanel(Static):
    """Full-screen ambient visuals ("w") meant to run behind whatever's playing, like a lofi
    player's animated backdrop — playback itself is untouched (AudioPlayerService keeps going
    regardless of what's on screen), this just swaps the whole UI for one of a few generative
    visuals instead of the normal panels. Cycled with ←/→ (state.wallpaper_visual).

    Each visual is computed as plain character rows plus per-row color spans rather than
    building a Rich Text cell-by-cell — a naive `Text.append` per character is easily tens of
    thousands of calls a frame at full-screen size, which doesn't hold up at the ~20fps the
    player's own tick loop repaints this at. Spans are cheap because most of a frame is either
    blank (starfield/rain) or made of same-colored runs (plasma's upscaled blocks)."""

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self._star_key: Optional[tuple] = None
        self._stars: list[tuple[float, float, float, float]] = []
        self._rain_key: Optional[int] = None
        self._rain_columns: list[tuple[float, float, int]] = []

    def refresh_content(self, accent: Optional[str] = None) -> None:
        s = self.state
        cols = max(1, int(self.size.width))
        rows = max(1, int(self.size.height))
        t = time.monotonic()

        renderers = {
            WallpaperVisual.SPECTRUM: self._render_spectrum,
            WallpaperVisual.STARFIELD: self._render_starfield,
            WallpaperVisual.RAIN: self._render_rain,
            WallpaperVisual.PLASMA: self._render_plasma,
        }
        row_strings, row_spans = renderers[s.wallpaper_visual](cols, rows, t, accent)
        row_strings = list(row_strings)
        row_spans = [list(spans) for spans in row_spans]
        self._overlay_now_playing(row_strings, row_spans, cols, rows)
        self.update(self._compose_frame(row_strings, row_spans))

    @staticmethod
    def _compose_frame(row_strings: list[str], row_spans: list[_RowSpans]) -> Text:
        text = Text()
        for i, (row, spans) in enumerate(zip(row_strings, row_spans)):
            if i:
                text.append("\n")
            line = Text(row)
            for start, end, style in spans:
                line.stylize(style, start, end)
            text.append(line)
        return text

    def _overlay_now_playing(
        self, row_strings: list[str], row_spans: list[_RowSpans], cols: int, rows: int
    ) -> None:
        """Replaces the last couple of rows with a caption — title/artist, position, and the
        exit/cycle hint — rather than drawing it on top of the visual, so it always stays legible
        regardless of what's happening behind it."""
        s = self.state
        if s.now_playing:
            lines = [
                f"♪ {s.now_playing.title} — {s.now_playing.artist}",
                f"{_format_seconds(s.position_seconds)} / {_format_seconds(s.duration_seconds)}",
            ]
        else:
            lines = ["Wallpaper mode", "Nothing playing — pick a track after pressing w again"]
        lines.append(f"w exit  ·  ←/→ visual: {s.wallpaper_visual.value}  ·  space pause")
        styles = ["bold white", "white", "dim"]

        start = max(0, rows - len(lines) - 1)
        for offset, (line, style) in enumerate(zip(lines, styles)):
            r = start + offset
            if not (0 <= r < rows):
                continue
            row_strings[r] = line[:cols].center(cols)
            row_spans[r] = [(0, cols, style)]

    # -- visuals -----------------------------------------------------------------------------

    def _render_spectrum(
        self, cols: int, rows: int, t: float, accent: Optional[str]
    ) -> tuple[list[str], list[_RowSpans]]:
        """Wide, mirrored equalizer bars driven by the real audio spectrum (AppState's own
        visualizer_levels — the same data BarVisualizerPanel uses, just spread full-screen and
        rainbow-shifted around the theme accent). Idles with a gentle breathing wave instead of
        flatlining while paused or before playback starts."""
        s = self.state
        levels = s.visualizer_levels
        n = len(levels)
        base_hue = _accent_hue(accent)
        mid = rows / 2.0

        grid = [[" "] * cols for _ in range(rows)]
        spans: list[_RowSpans] = [[] for _ in range(rows)]

        for col in range(cols):
            if n and s.is_playing:
                level = levels[min(n - 1, col * n // cols)]
            else:
                level = (math.sin(t * 1.4 + col * 0.18) * 0.5 + 0.5) * 22
            half_height = int(level / 100 * mid)
            if half_height <= 0:
                continue
            color = _hsv_hex(base_hue + col / cols * 0.5, 0.65, 0.95)
            for h in range(half_height):
                for r in (int(mid) - 1 - h, int(mid) + h):
                    if 0 <= r < rows:
                        grid[r][col] = "█"
                        spans[r].append((col, col + 1, color))

        return ["".join(row) for row in grid], spans

    def _ensure_stars(self, cols: int, rows: int) -> None:
        key = (cols, rows)
        if key == self._star_key:
            return
        rng = random.Random(cols * 10_000 + rows)
        count = max(12, (cols * rows) // 45)
        self._stars = [
            (rng.uniform(0, cols), rng.uniform(0, rows), rng.uniform(1.5, 5.0), rng.uniform(0, math.tau))
            for _ in range(count)
        ]
        self._star_key = key

    def _render_starfield(
        self, cols: int, rows: int, t: float, accent: Optional[str]
    ) -> tuple[list[str], list[_RowSpans]]:
        """Slow-drifting, twinkling stars — a classic screensaver, purely time-driven so it stays
        alive even while playback is paused."""
        self._ensure_stars(cols, rows)
        grid = [[" "] * cols for _ in range(rows)]
        spans: list[_RowSpans] = [[] for _ in range(rows)]

        for x, y0, speed, phase in self._stars:
            y = int((y0 + t * speed * 0.4) % rows)
            x_i = int(x) % cols
            twinkle = math.sin(t * 2 + phase)
            if twinkle > 0.6:
                char, style = "*", f"bold {accent}" if accent else "bold white"
            elif twinkle > -0.2:
                char, style = "+", "grey70"
            else:
                char, style = ".", "grey42"
            grid[y][x_i] = char
            spans[y].append((x_i, x_i + 1, style))

        return ["".join(row) for row in grid], spans

    def _ensure_rain(self, cols: int) -> None:
        if cols == self._rain_key:
            return
        rng = random.Random(cols)
        self._rain_columns = [
            (rng.uniform(0, 1000), rng.uniform(6.0, 14.0), rng.randint(3, len(_RAIN_CHARS) + 2))
            for _ in range(cols)
        ]
        self._rain_key = cols

    def _render_rain(
        self, cols: int, rows: int, t: float, accent: Optional[str]
    ) -> tuple[list[str], list[_RowSpans]]:
        """Sparse falling character streams, muted/tinted with the theme accent rather than the
        usual matrix green — a quieter, lofi-appropriate rain instead of a hacker-movie effect."""
        self._ensure_rain(cols)
        grid = [[" "] * cols for _ in range(rows)]
        spans: list[_RowSpans] = [[] for _ in range(rows)]

        for col, (offset, speed, length) in enumerate(self._rain_columns):
            head = (offset + t * speed) % (rows + length)
            for i in range(length):
                y = int(head) - i
                if not (0 <= y < rows):
                    continue
                grid[y][col] = _RAIN_CHARS[min(i, len(_RAIN_CHARS) - 1)]
                if i == 0:
                    style = f"bold {accent}" if accent else "bold white"
                elif i < length / 2:
                    style = "grey70"
                else:
                    style = "grey35"
                spans[y].append((col, col + 1, style))

        return ["".join(row) for row in grid], spans

    def _render_plasma(
        self, cols: int, rows: int, t: float, accent: Optional[str]
    ) -> tuple[list[str], list[_RowSpans]]:
        """Chunky, retro demoscene-style plasma — computed on a small fixed grid and upscaled
        with block characters (nearest-neighbor) rather than one color per terminal cell, both to
        keep the per-frame trig/HSV cost independent of the actual terminal size and because the
        pixelated look reads as more "lofi" than a smooth gradient would anyway."""
        base_hue = _accent_hue(accent)
        gw, gh = min(_PLASMA_GRID_W, cols), min(_PLASMA_GRID_H, rows)

        cell_colors = [[""] * gw for _ in range(gh)]
        for gy in range(gh):
            for gx in range(gw):
                v = (
                    math.sin(gx * 0.35 + t * 0.8)
                    + math.sin(gy * 0.5 + t * 0.6)
                    + math.sin((gx + gy) * 0.25 + t)
                    + math.sin(math.hypot(gx - gw / 2, gy - gh / 2) * 0.3 - t * 1.2)
                ) / 4.0
                hue = base_hue + (v + 1) / 2 * 0.35
                value = 0.5 + (v + 1) / 2 * 0.4
                cell_colors[gy][gx] = _hsv_hex(hue, 0.7, value)

        row_strings = []
        row_spans: list[_RowSpans] = []
        for row in range(rows):
            gy = min(gh - 1, row * gh // rows)
            spans: _RowSpans = []
            run_start = 0
            run_color = cell_colors[gy][0]
            for col in range(1, cols):
                color = cell_colors[gy][min(gw - 1, col * gw // cols)]
                if color != run_color:
                    spans.append((run_start, col, run_color))
                    run_start, run_color = col, color
            spans.append((run_start, cols, run_color))
            row_strings.append("█" * cols)
            row_spans.append(spans)

        return row_strings, row_spans


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
    ("v", "single view"),
    ("m", "minimal"),
    ("w", "wallpaper"),
    ("o", "sign out"),
    ("q", "quit"),
]

# "x remove" only does anything with the Queue panel focused, so it's spliced into the hint bar
# there instead of always showing — matches _KEY_ACTIONS' own position for where it'd otherwise
# sit, right after next/prev.
_QUEUE_REMOVE_ACTION = ("x", "remove")

# "←/→ switch panel" only means anything once single-view ("v") is on — the sidebar's three
# panels are all visible at once otherwise, so there's nothing to switch between.
_PAGED_NAV_ACTION = ("←/→", "switch panel")


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
        if self.state.is_sidebar_paged:
            actions.insert(1, _PAGED_NAV_ACTION)

        divider = "   "
        pairs = (f"[b reverse] {key} [/]  {action}" for key, action in actions)
        self.update(divider.join(pairs))
