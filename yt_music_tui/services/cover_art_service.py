from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import numpy as np

"""
Fetches a track's thumbnail and decodes it to a small raw-RGB pixel grid via ffmpeg (already
a hard dependency for playback, so this needs no image library). CoverArtPanel turns the grid
into half-block ANSI art at render time.
"""

MAX_WIDTH = 160


class CoverArtService:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)

    async def fetch(self, url: str) -> Optional[np.ndarray]:
        try:
            response = await self._client.get(url)
            if response.status_code != 200:
                return None
        except Exception:
            return None

        return await self._decode(response.content)

    async def _decode(self, data: bytes) -> Optional[np.ndarray]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-vf", f"scale={MAX_WIDTH}:-2", "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate(input=data)
        except Exception:
            return None

        width = MAX_WIDTH
        height = len(out) // (width * 3)
        if height <= 0:
            return None

        pixels = np.frombuffer(out, dtype=np.uint8)[: width * height * 3]
        return pixels.reshape(height, width, 3).copy()

    async def aclose(self) -> None:
        await self._client.aclose()
