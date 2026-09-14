from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: Optional[str] = None
    duration: Optional[timedelta] = None
    thumbnail_url: Optional[str] = None


@dataclass(frozen=True)
class Playlist:
    id: str
    title: str
    description: Optional[str] = None
    track_count: int = 0


@dataclass(frozen=True)
class SearchResults:
    tracks: List[Track] = field(default_factory=list)


@dataclass(frozen=True)
class LyricsLine:
    time: Optional[timedelta]
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: List[LyricsLine]
    is_synced: bool


NOT_FOUND_LYRICS = Lyrics(lines=[LyricsLine(None, "No lyrics found for this track.")], is_synced=False)
