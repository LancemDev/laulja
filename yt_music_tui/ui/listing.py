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
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._body = Static()

    def compose(self):
        yield self._body

    def set_lines(self, renderable: RenderableType, selected_index: Optional[int], total: int) -> None:
        self._body.update(renderable)
        if selected_index is not None and total > 0:
            self.call_after_refresh(self._reveal, selected_index)

    def _reveal(self, index: int) -> None:
        height = max(1, int(self.size.height))
        top = int(self.scroll_y)
        if index < top:
            self.scroll_to(y=index, animate=False)
        elif index >= top + height:
            self.scroll_to(y=index - height + 1, animate=False)
