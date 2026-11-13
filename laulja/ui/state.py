from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

from ..models import Lyrics, Playlist, Track


class LeftFocus(Enum):
    TRACKS = "tracks"
    PLAYLISTS = "playlists"
    QUEUE = "queue"


class FullScreenMode(Enum):
    NONE = "none"
    COVER_BAR = "cover_bar"
    LYRICS = "lyrics"


class WallpaperVisual(Enum):
    """The selectable generative backdrops for wallpaper mode ("w") — cycled with ←/→ while
    active. SPECTRUM is audio-reactive (it reads AppState.visualizer_levels), so its ridge holds
    still while paused; the others are purely time-driven so they keep animating even then."""

    SPECTRUM = "spectrum"
    AURORA = "aurora"
    RIPPLE = "ripple"
    ORBIT = "orbit"


@dataclass
class AppState:
    status_message: str = "Ready"
    search_query: str = ""
    is_searching: bool = False
    is_showing_search_results: bool = False
    is_showing_playlist_search_results: bool = False
    # True while a search (tracks or playlists) is in flight — distinguishes "still fetching"
    # from "fetched and genuinely empty" in the Tracks/Playlists panels.
    is_search_loading: bool = False
    # Which panel a search was launched from (Tracks vs Playlists — Queue counts as Tracks,
    # since there's no such thing as "searching the queue"), snapshotted when '/' is pressed
    # rather than re-read from left_focus later, so the typing indicator and submit both agree
    # on where the search is headed even though left_focus itself can't change mid-search.
    search_target: LeftFocus = LeftFocus.TRACKS

    left_focus: LeftFocus = LeftFocus.TRACKS
    full_screen_mode: FullScreenMode = FullScreenMode.NONE
    is_sidebar_collapsed: bool = False
    # Normally Tracks/Queue/Playlists all show at once, stacked and height-sliced. Paged mode
    # instead shows only whichever one is left_focus, at the sidebar's full height — trading
    # "see all three" for "see one long list without scrolling", navigated with left/right.
    is_sidebar_paged: bool = True
    is_minimal: bool = False
    is_loading_library: bool = True
    # Sidebar/lyrics widths as percentages of the body, so they keep their proportion when the
    # terminal is enlarged instead of staying at a fixed column count. Dragging a divider
    # updates these (see PaneSplitter).
    left_pane_width_pct: float = 22.0
    lyrics_pane_width_pct: float = 26.0

    # Full-screen ambient visuals ("w") that run behind whatever's currently playing, like a
    # lofi player's animated backdrop — the sidebar/lyrics/cover panels are hidden entirely
    # rather than just full-screened, since the point is the visual, not any of that content.
    is_wallpaper_mode: bool = False
    wallpaper_visual: WallpaperVisual = WallpaperVisual.SPECTRUM

    tracks_selected_index: int = 0
    playlists_selected_index: int = 0
    queue_selected_index: int = 0

    library_tracks: List[Track] = field(default_factory=list)
    search_results: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    playlist_search_results: List[Playlist] = field(default_factory=list)
    queue: List[Track] = field(default_factory=list)

    # Set when a playlist (or the synthetic "Liked Songs" one — see app.py's
    # LIKED_SONGS_PLAYLIST_ID) has been opened for browsing via Enter, mirroring YT Music's own
    # "open the playlist page" behavior instead of queuing + playing it immediately. None means
    # the Playlists panel is still showing the list of playlists rather than one playlist's
    # tracks.
    viewing_playlist: Optional[Playlist] = None
    playlist_view_tracks: List[Track] = field(default_factory=list)
    playlist_view_selected_index: int = 0
    # Set when the view was opened from an album search result, so Esc returns there.
    playlist_view_return_focus: Optional[LeftFocus] = None

    now_playing: Optional[Track] = None
    is_playing: bool = False
    # Playback is waiting for audio to arrive (start-up, or after the network stalled).
    is_buffering: bool = False
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    visualizer_levels: List[int] = field(default_factory=list)
    lyrics: Optional[Lyrics] = None
    cover_art: Optional[bytes] = None

    # Seeded from get_liked_songs() at startup, then updated optimistically on 'l' rather than
    # re-fetched from the API each time — ytmusicapi has no per-track "is this liked" lookup,
    # only the bulk get_liked_songs() list.
    liked_track_ids: Set[str] = field(default_factory=set)

    # Tracks whose audio has been downloaded to the machine's Downloads folder for offline
    # playback. Populated optimistically as downloads complete; a restart treats everything as
    # not downloaded (files are re-detected lazily when played).
    downloaded_track_ids: Set[str] = field(default_factory=set)
    # Tracks currently being downloaded or queued for download.
    downloading_track_ids: Set[str] = field(default_factory=set)
    # Per-track download progress, 0.0–1.0, synced from DownloadService on each tick.
    download_progress: Dict[str, float] = field(default_factory=dict)
    # Last error per track id, shown so a failed download is visible rather than silent.
    download_errors: Dict[str, str] = field(default_factory=dict)

    @property
    def download_count(self) -> int:
        """Tracks whose download has finished."""
        return len(self.downloaded_track_ids)

    @property
    def download_active_count(self) -> int:
        """Tracks being downloaded right now."""
        return len(self.downloading_track_ids)

    @property
    def download_queued_count(self) -> int:
        """Tracks waiting for a download slot (active_count + queued_count from the service)."""
        return len(self.downloading_track_ids) - sum(
            1 for p in self.download_progress.values() if p >= 1.0
        )

    def is_track_downloaded(self, track_id: str) -> bool:
        return track_id in self.downloaded_track_ids

    def is_track_downloading(self, track_id: str) -> bool:
        return track_id in self.downloading_track_ids

    def track_download_progress(self, track_id: str) -> float:
        """Download progress for `track_id`, 0.0–1.0."""
        return self.download_progress.get(track_id, 0.0)

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
