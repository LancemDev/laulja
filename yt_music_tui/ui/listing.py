from __future__ import annotations

from typing import Optional

from rich.console import RenderableType
from textual.containers import VerticalScroll
from textual.widgets import Static


class ListPanel(VerticalScroll):
    """A bordered, scrollable panel that renders a list of lines with one row highlighted —
    the Python/Textual stand-in for Ratatui's `List` widget (used by TracksPanel,
    PlaylistsPanel, and LyricsPanel in the original)."""

    DEFAULT_CSS = """
    ListPanel {
        border: round $primary;
        padding: 0 1;

        /* Textual's default scrollbar is 2 cells wide, grey, and unthemed — it reads as a
           generic OS widget bolted onto a rounded, theme-colored panel. Slim it to 1 cell and
           tint it with the app's own colors (which now shift with the album art) so it reads
           as part of the panel instead of competing with it. */
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
        scrollbar-background: $surface;
        scrollbar-background-hover: $surface;
        scrollbar-background-active: $surface;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._body = Static()

    def compose(self):
        yield self._body

    def set_lines(
        self,
        renderable: RenderableType,
        selected_index: Optional[int],
        total: int,
        *,
        center: bool = False,
        animate: bool = False,
    ) -> None:
        self._body.update(renderable)
        if selected_index is None or total <= 0:
            return
        if center:
            self.call_after_refresh(self._reveal_centered, selected_index, animate)
        else:
            self.call_after_refresh(self._reveal, selected_index)

    def _reveal(self, index: int) -> None:
        height = max(1, int(self.size.height))
        top = int(self.scroll_y)
        if index < top:
            self.scroll_to(y=index, animate=False)
        elif index >= top + height:
            self.scroll_to(y=index - height + 1, animate=False)

    def _reveal_centered(self, index: int, animate: bool) -> None:
        # Keeps the active line anchored near the middle of the panel, so each advance drifts
        # the whole list up by one row — a continuous scroll rather than a hard cut.
        height = max(1, int(self.size.height))
        target = max(0, index - height // 2)
        self.scroll_to(y=target, animate=animate, duration=0.35, easing="out_cubic")
