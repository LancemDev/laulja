from __future__ import annotations

from typing import Optional

from rich.console import RenderableType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static


class ListPanel(Vertical):
    """A scrollable panel that renders a list of lines with one row highlighted — the
    Python/Textual stand-in for Ratatui's `List` widget (used by TracksPanel, PlaylistsPanel,
    and LyricsPanel in the original). A left accent bar marks the panel instead of a full box;
    since that leaves no border row to embed a title into, the title is a plain header line
    above a nested scrollable body instead of `border_title`."""

    DEFAULT_CSS = """
    ListPanel {
        border-left: wide $primary;
        padding: 0 1;
    }
    ListPanel > #list-header {
        height: 1;
        text-style: bold;
    }
    ListPanel > #list-scroll {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._header = Static(id="list-header")
        self._body = Static()
        self._scroll = VerticalScroll(self._body, id="list-scroll")

    def compose(self):
        yield self._header
        yield self._scroll

    def set_title(self, title: RenderableType) -> None:
        self._header.update(title)

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
        height = max(1, int(self._scroll.size.height))
        top = int(self._scroll.scroll_y)
        if index < top:
            self._scroll.scroll_to(y=index, animate=False)
        elif index >= top + height:
            self._scroll.scroll_to(y=index - height + 1, animate=False)

    def _reveal_centered(self, index: int, animate: bool) -> None:
        # Keeps the active line anchored near the middle of the panel, so each advance drifts
        # the whole list up by one row — a continuous scroll rather than a hard cut.
        height = max(1, int(self._scroll.size.height))
        target = max(0, index - height // 2)
        self._scroll.scroll_to(y=target, animate=animate, duration=0.35, easing="out_cubic")
