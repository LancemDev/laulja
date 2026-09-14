from __future__ import annotations

import re
from datetime import timedelta
from typing import List, Optional

import httpx

from ..models import Lyrics, LyricsLine, NOT_FOUND_LYRICS

"""
Fetches synced/plain lyrics from LRCLIB (lrclib.net) — free, keyless, community-sourced.
ytmusicapi has no lyrics endpoint that doesn't require extra scraping. Mirrors LyricsService.cs.
"""

_TIME_TAG = re.compile(r"^\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)$")


class LyricsService:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url="https://lrclib.net/", timeout=8.0)

    async def get_lyrics(self, title: str, artist: str, duration: Optional[timedelta]) -> Lyrics:
        try:
            params: dict[str, str | int] = {"track_name": title, "artist_name": artist}
            if duration is not None:
                params["duration"] = int(duration.total_seconds())

            response = await self._client.get("api/get", params=params)
            if response.status_code != 200:
                return NOT_FOUND_LYRICS

            payload = response.json()
            synced = payload.get("syncedLyrics")
            plain = payload.get("plainLyrics")

            if synced and synced.strip():
                return Lyrics(lines=_parse_synced(synced), is_synced=True)

            if plain and plain.strip():
                lines = [LyricsLine(None, line) for line in plain.split("\n")]
                return Lyrics(lines=lines, is_synced=False)

            return NOT_FOUND_LYRICS
        except Exception:
            return NOT_FOUND_LYRICS

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_synced(lrc: str) -> List[LyricsLine]:
    lines: List[LyricsLine] = []
    for raw in lrc.split("\n"):
        match = _TIME_TAG.match(raw.rstrip("\r"))
        if not match:
            continue

        minutes, seconds, millis_raw, text = match.groups()
        millis = int((millis_raw + "00")[:3])
        time = timedelta(minutes=int(minutes), seconds=int(seconds), milliseconds=millis)
        lines.append(LyricsLine(time, text if text.strip() else " "))

    return lines if lines else list(NOT_FOUND_LYRICS.lines)
