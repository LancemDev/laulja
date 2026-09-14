from __future__ import annotations

import io
from typing import Optional

import httpx
from PIL import Image as PILImage

"""
Fetches a track's thumbnail. CoverArtPanel hands the raw bytes straight to textual-image,
which renders it as a real bitmap via the terminal's own graphics protocol (Kitty/Sixel) when
available, falling back to colored half-cells or plain unicode otherwise — no manual decoding
needed here beyond a sanity check that the bytes are actually a valid image.
"""


class CoverArtService:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)

    async def fetch(self, url: str) -> Optional[bytes]:
        try:
            response = await self._client.get(url)
            if response.status_code != 200:
                return None
        except Exception:
            return None

        data = response.content
        try:
            with PILImage.open(io.BytesIO(data)) as img:
                img.load()
        except Exception:
            return None

        return data

    async def aclose(self) -> None:
        await self._client.aclose()
