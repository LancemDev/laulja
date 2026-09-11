from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set

from ..models import Lyrics, Playlist, Track


class LeftFocus(Enum):
    TRACKS = "tracks"
    PLAYLISTS = "playlists"
    QUEUE = "queue"


class FullScreenMode(Enum):
    NONE = "none"
    COVER_BAR = "cover_bar"
    LYRICS = "lyrics"


@dataclass
class AppState:
    status_message: str = "Ready"
    search_query: str = ""
    is_searching: bool = False
    is_showing_search_results: bool = False
    is_showing_playlist_search_results: bool = False
    # Which panel a search was launched from (Tracks vs Playlists — Queue counts as Tracks,
    # since there's no such thing as "searching the queue"), snapshotted when '/' is pressed
    # rather than re-read from left_focus later, so the typing indicator and submit both agree
    # on where the search is headed even though left_focus itself can't change mid-search.
    search_target: LeftFocus = LeftFocus.TRACKS

    left_focus: LeftFocus = LeftFocus.TRACKS
    full_screen_mode: FullScreenMode = FullScreenMode.NONE
    is_sidebar_collapsed: bool = False
    is_minimal: bool = False
    is_loading_library: bool = True

    tracks_selected_index: int = 0
    playlists_selected_index: int = 0
    queue_selected_index: int = 0

    library_tracks: List[Track] = field(default_factory=list)
    search_results: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    playlist_search_results: List[Playlist] = field(default_factory=list)
    queue: List[Track] = field(default_factory=list)

    now_playing: Optional[Track] = None
    is_playing: bool = False
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    visualizer_levels: List[int] = field(default_factory=list)
    lyrics: Optional[Lyrics] = None
    cover_art: Optional[bytes] = None

    # Seeded from get_liked_songs() at startup, then updated optimistically on 'l' rather than
    # re-fetched from the API each time — ytmusicapi has no per-track "is this liked" lookup,
    # only the bulk get_liked_songs() list.
    liked_track_ids: Set[str] = field(default_factory=set)

    auth_label: str = "not signed in"
    is_authenticated: bool = False

    @property
    def displayed_tracks(self) -> List[Track]:
        return self.search_results if self.is_showing_search_results else self.library_tracks

    @property
    def displayed_playlists(self) -> List[Playlist]:
        return self.playlist_search_results if self.is_showing_playlist_search_results else self.playlists

    @property
    def progress_ratio(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position_seconds / self.duration_seconds))
