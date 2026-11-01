from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ytmusicapi import YTMusic

from ..auth.cookies import Cookie, to_netscape
from ..models import ArtistRef, Playlist, SearchResults, Track
from .blocklist import ArtistBlocklist

"""
Wraps ytmusicapi (library/search/playlists — ytmusicapi has no streaming endpoint) and
yt-dlp (stream URL resolution) behind async methods. Mirrors YouTubeMusicService.cs, with
yt-dlp standing in for the C# project's YouTubeMusicAPI + YouTubeSessionGenerator pairing:
yt-dlp resolves a playable audio URL — and handles YouTube's PoToken/BotGuard requirements
internally — without needing a local Node.js process.
"""


def _parse_length(length: Optional[str]) -> Optional[timedelta]:
    """Parses the "m:ss" / "h:mm:ss" duration string ytmusicapi's watch-playlist endpoint
    (get_watch_playlist, used for radio/mix queues) reports instead of search's plain
    duration_seconds int."""
    if not length:
        return None
    try:
        parts = [int(p) for p in length.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return timedelta(hours=h, minutes=m, seconds=sec)


def _track_duration(song: Dict[str, Any]) -> Optional[timedelta]:
    seconds = song.get("duration_seconds")
    if seconds:
        return timedelta(seconds=seconds)
    return _parse_length(song.get("length"))


def _artist_refs(artists: Optional[List[Dict[str, Any]]]) -> tuple[ArtistRef, ...]:
    return tuple(
        ArtistRef(name=a["name"], id=a.get("id") or None)
        for a in (artists or [])
        if a and a.get("name")
    )


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
    artists = _artist_refs(song.get("artists"))
    return Track(
        id=video_id,
        title=song.get("title") or "Unknown",
        artist=", ".join(a.name for a in artists) if artists else "Unknown Artist",
        artists=artists,
        album=_album_name(song.get("album")),
        duration=_track_duration(song),
        # Search results key thumbnails as "thumbnails"; watch-playlist (radio/mix) tracks key
        # the same shape as singular "thumbnail" instead.
        thumbnail_url=_thumbnail(song.get("thumbnails") or song.get("thumbnail")),
    )


def _to_playlist(item: Dict[str, Any]) -> Optional[Playlist]:
    # Search's "playlist" result keys the id as "browseId" ("VL" + the real playlist id, or
    # already "VL"-prefixed for some results) rather than search-songs' "videoId"/library's bare
    # "playlistId" — but get_playlist() accepts either form, so no stripping needed here.
    playlist_id = item.get("browseId")
    if not playlist_id:
        return None
    count = item.get("itemCount")
    return Playlist(
        id=playlist_id,
        title=item.get("title") or "Untitled",
        description=item.get("author"),
        track_count=count if isinstance(count, int) else 0,
    )


def _to_album(item: Dict[str, Any]) -> Optional[Track]:
    browse_id = item.get("browseId")
    if not browse_id:
        return None
    artists = _artist_refs(item.get("artists"))
    title = item.get("title") or "Untitled"
    return Track(
        id=browse_id,
        title=title,
        artist=", ".join(a.name for a in artists) if artists else "Unknown Artist",
        artists=artists,
        album=title,
        thumbnail_url=_thumbnail(item.get("thumbnails")),
        is_album=True,
    )


def _words(text: str) -> List[str]:
    return re.findall(r"[0-9a-z]+", text.casefold())


def _match_score(query: str, item: Track) -> int:
    """How well a result fits the query, comparing the title (and title + artist) word-wise:
    an exact title match beats the query appearing inside the title, which beats merely
    containing every query word somewhere across title and artist."""
    q = _words(query)
    if not q:
        return 0
    title = _words(item.title)
    full = title + _words(item.artist)
    if title == q or full == q:
        return 4
    joined_q, joined_title = " ".join(q), " ".join(title)
    if joined_title.startswith(joined_q):
        return 3
    if joined_q in joined_title or joined_q in " ".join(full):
        return 2
    if set(q) <= set(full):
        return 1
    return 0


def _rank_results(
    query: str, songs: List[Track], albums: List[Track], top: Optional[Track]
) -> List[Track]:
    """Merges songs and albums into one list ordered by best fit. YouTube Music's own "top
    result" goes first; the rest are sorted by match score, with ties broken by each item's
    position within its own (already relevance-ordered) list so neither kind floods the top."""
    ranked = sorted(
        [(t, i / max(len(songs), 1)) for i, t in enumerate(songs)]
        + [(a, i / max(len(albums), 1)) for i, a in enumerate(albums)],
        key=lambda pair: (-_match_score(query, pair[0]), pair[1]),
    )
    merged = [t for t, _ in ranked]
    if top is not None:
        merged = [top] + [t for t in merged if t.id != top.id]
    return merged


class MusicService:
    """Thin async wrapper around ytmusicapi — a blocking library run in worker threads."""

    def __init__(
        self,
        client: YTMusic,
        cookies: Optional[List[Cookie]] = None,
        blocklist: Optional[ArtistBlocklist] = None,
    ):
        self._client = client
        self._cookies = list(cookies or [])
        # Applied to recommendations (radio) and search only — library and playlist contents are
        # the user's own picks, so they're left as-is.
        self._blocklist = blocklist

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
        """Songs and albums in one list, best fit first (see _rank_results)."""
        song_results, album_results, top_results = await asyncio.gather(
            asyncio.to_thread(self._client.search, query, "songs", None, 25),
            asyncio.to_thread(self._client.search, query, "albums", None, 10),
            asyncio.to_thread(self._client.search, query, None, None, 1),
        )
        songs = [t for t in (_to_track(r) for r in song_results) if t is not None]
        # Only the first few: past those, album search is mostly same-titled knock-offs and
        # compilations that would otherwise tie with (and bury) the real songs.
        albums = [a for a in (_to_album(r) for r in album_results) if a is not None][:3]
        top = self._top_result(top_results)
        merged = _rank_results(query, songs[:25], albums, top)
        tracks, hidden = self._apply_blocklist(merged)
        return SearchResults(tracks=tracks, hidden=hidden)

    @staticmethod
    def _top_result(results: List[Dict[str, Any]]) -> Optional[Track]:
        for r in results:
            if r.get("category") != "Top result":
                continue
            if r.get("resultType") == "song":
                return _to_track(r)
            if r.get("resultType") == "album":
                return _to_album(r)
            return None
        return None

    async def search_playlists(self, query: str) -> List[Playlist]:
        results = await asyncio.to_thread(self._client.search, query, "playlists", None, 25)
        playlists = [p for p in (_to_playlist(r) for r in results) if p is not None]
        return playlists[:25]

    async def get_album_tracks(self, browse_id: str) -> List[Track]:
        album = await asyncio.to_thread(self._client.get_album, browse_id)
        title = album.get("title")
        # Album track entries carry no artwork of their own; use the album cover.
        cover = _thumbnail(album.get("thumbnails"))
        tracks = []
        for item in album.get("tracks", []):
            track = _to_track(item)
            if track is None:
                continue
            tracks.append(
                replace(
                    track,
                    album=track.album or title,
                    thumbnail_url=track.thumbnail_url or cover,
                )
            )
        return tracks

    async def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        playlist = await asyncio.to_thread(self._client.get_playlist, playlist_id, None)
        return [t for t in (_to_track(item) for item in playlist.get("tracks", [])) if t is not None]

    async def get_liked_song_ids(self) -> set[str]:
        liked = await asyncio.to_thread(self._client.get_liked_songs, 200)
        return {item["videoId"] for item in liked.get("tracks", []) if item.get("videoId")}

    async def rate_song(self, track_id: str, liked: bool) -> None:
        rating = "LIKE" if liked else "INDIFFERENT"
        await asyncio.to_thread(self._client.rate_song, track_id, rating)

    async def get_radio_tracks(self, track_id: str, limit: int = 25) -> List[Track]:
        """Starts a YouTube Music "radio" (what the web/app UI calls a Mix) seeded from
        `track_id` — the same continuous, similar-vibe queue YouTube Music builds for you when
        you play an individual song outside a playlist. The seed track is included first."""
        result = await asyncio.to_thread(self._client.get_watch_playlist, track_id, None, limit, True)
        tracks = result.get("tracks") or []
        return self._apply_blocklist([t for t in (_to_track(item) for item in tracks) if t is not None])[0]

    def _apply_blocklist(self, tracks: List[Track]) -> tuple[List[Track], int]:
        if self._blocklist is None:
            return tracks, 0
        return self._blocklist.filter(tracks)

    async def get_stream_url(self, track_id: str, low_bandwidth: bool = False) -> str:
        return await asyncio.to_thread(_resolve_stream_url, track_id, low_bandwidth, self._cookies)


# Highest quality normally; on a slow connection, the best stream at or under ~70kbps (YouTube's
# 48-70kbps opus tiers) instead, falling back to the smallest stream available if none of those
# exist for a track — a stream the connection can actually keep up with beats a better one that
# stalls.
_FORMAT_BEST = "bestaudio/best"
_FORMAT_LOW_BANDWIDTH = "bestaudio[abr<=70]/worstaudio/bestaudio/best"


_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def _available_js_runtimes() -> Dict[str, Dict[str, Any]]:
    # YouTube now gates stream URLs behind a JS challenge that yt-dlp solves with an external
    # runtime (plus the yt-dlp-ejs solver package). yt-dlp only tries deno by default, so enable
    # whichever supported runtimes are actually installed.
    return {name: {} for name in _JS_RUNTIMES if shutil.which(name)}


def _resolve_stream_url(track_id: str, low_bandwidth: bool = False, cookies: Optional[List[Cookie]] = None) -> str:
    import yt_dlp

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "format": _FORMAT_LOW_BANDWIDTH if low_bandwidth else _FORMAT_BEST,
        "noplaylist": True,
        "skip_download": True,
    }
    runtimes = _available_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    # Anonymous requests get "Sign in to confirm you're not a bot", so hand yt-dlp the same
    # session cookies ytmusicapi uses. A fresh owner-only file per call, since yt-dlp writes the
    # jar back on close and the prefetch resolve can run concurrently with the current one.
    cookie_path: Optional[str] = None
    if cookies:
        fd, cookie_path = tempfile.mkstemp(prefix="laulja-cookies-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(to_netscape(cookies))
        opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={track_id}", download=False)
            url = info.get("url") if info else None
            if not url and info and info.get("requested_formats"):
                url = info["requested_formats"][0].get("url")
            if not url:
                raise RuntimeError(f"No audio stream available for track {track_id}.")
            return url
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass
