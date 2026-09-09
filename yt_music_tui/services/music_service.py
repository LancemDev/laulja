from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ytmusicapi import YTMusic

from ..models import Playlist, SearchResults, Track

"""
Wraps ytmusicapi (library/search/playlists — ytmusicapi has no streaming endpoint) and
yt-dlp (stream URL resolution) behind async methods. Mirrors YouTubeMusicService.cs, with
yt-dlp standing in for the C# project's YouTubeMusicAPI + YouTubeSessionGenerator pairing:
yt-dlp resolves a playable audio URL — and handles YouTube's PoToken/BotGuard requirements
internally — without needing a local Node.js process.
"""


def _duration(seconds: Optional[int]) -> Optional[timedelta]:
    return timedelta(seconds=seconds) if seconds else None


def _artist_names(artists: Optional[List[Dict[str, Any]]]) -> str:
    names = [a.get("name") for a in (artists or []) if a and a.get("name")]
    return ", ".join(names) if names else "Unknown Artist"


def _thumbnail(thumbnails: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not thumbnails:
        return None
    return max(thumbnails, key=lambda t: t.get("width", 0) or 0).get("url")


def _album_name(album: Any) -> Optional[str]:
    if isinstance(album, dict):
        return album.get("name")
    return album if isinstance(album, str) else None


def _to_track(song: Dict[str, Any]) -> Optional[Track]:
    video_id = song.get("videoId")
    if not video_id:
        return None
    return Track(
        id=video_id,
        title=song.get("title") or "Unknown",
        artist=_artist_names(song.get("artists")),
        album=_album_name(song.get("album")),
        duration=_duration(song.get("duration_seconds")),
        thumbnail_url=_thumbnail(song.get("thumbnails")),
    )


class MusicService:
    """Thin async wrapper around ytmusicapi — a blocking library run in worker threads."""

    def __init__(self, client: YTMusic):
        self._client = client

    async def get_library_tracks(self) -> List[Track]:
        songs = await asyncio.to_thread(self._client.get_library_songs, 200)
        return [t for t in (_to_track(s) for s in songs) if t is not None]

    async def get_library_playlists(self) -> List[Playlist]:
        playlists = await asyncio.to_thread(self._client.get_library_playlists, 100)
        return [
            Playlist(id=p["playlistId"], title=p.get("title") or "Untitled", track_count=p.get("count") or 0)
            for p in playlists
            if p.get("playlistId")
        ]

    async def search(self, query: str) -> SearchResults:
        results = await asyncio.to_thread(self._client.search, query, "songs", None, 25)
        tracks = [t for t in (_to_track(r) for r in results) if t is not None]
        return SearchResults(tracks=tracks[:25])

    async def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        playlist = await asyncio.to_thread(self._client.get_playlist, playlist_id, None)
        return [t for t in (_to_track(item) for item in playlist.get("tracks", [])) if t is not None]

    async def get_liked_song_ids(self) -> set[str]:
        liked = await asyncio.to_thread(self._client.get_liked_songs, 200)
        return {item["videoId"] for item in liked.get("tracks", []) if item.get("videoId")}

    async def rate_song(self, track_id: str, liked: bool) -> None:
        rating = "LIKE" if liked else "INDIFFERENT"
        await asyncio.to_thread(self._client.rate_song, track_id, rating)

    async def get_stream_url(self, track_id: str) -> str:
        return await asyncio.to_thread(_resolve_stream_url, track_id)


def _resolve_stream_url(track_id: str) -> str:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://music.youtube.com/watch?v={track_id}", download=False)
        url = info.get("url") if info else None
        if not url and info and info.get("requested_formats"):
            url = info["requested_formats"][0].get("url")
        if not url:
            raise RuntimeError(f"No audio stream available for track {track_id}.")
        return url
