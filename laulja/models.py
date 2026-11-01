from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ArtistRef:
    name: str
    # YouTube channel ID ("UC…"), when the API reported one.
    id: Optional[str] = None


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: Optional[str] = None
    duration: Optional[timedelta] = None
    thumbnail_url: Optional[str] = None
    # The individual artists behind the joined `artist` display string.
    artists: Tuple[ArtistRef, ...] = ()
    # Set when this search-result entry is an album rather than a song: `id` is then the
    # album's browseId ("MPREb_…"), and Enter opens it instead of playing it.
    is_album: bool = False


@dataclass(frozen=True)
class Playlist:
    id: str
    title: str
    description: Optional[str] = None
    track_count: int = 0
    # Albums share the playlist list/view; `id` is then the album's browseId ("MPREb_…").
    is_album: bool = False


@dataclass(frozen=True)
class SearchResults:
    tracks: List[Track] = field(default_factory=list)
    # Results dropped by the artist blocklist.
    hidden: int = 0


@dataclass(frozen=True)
class LyricsLine:
    time: Optional[timedelta]
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: List[LyricsLine]
    is_synced: bool


NOT_FOUND_LYRICS = Lyrics(lines=[LyricsLine(None, "No lyrics found for this track.")], is_synced=False)
