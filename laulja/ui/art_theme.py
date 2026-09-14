from __future__ import annotations

import colorsys
import io
from typing import List, Optional, Tuple

from PIL import Image as PILImage
from textual.theme import Theme

"""
Builds a Textual Theme from a track's cover art, so the whole UI's colors (borders, accents,
background) shift to match whatever's currently playing — the same idea as Spotify's ambient
"Canvas" background, adapted to a terminal's flat color roles (primary/accent/background/...).
"""

THEME_NAME = "cover-art"

_SAMPLE_SIZE = 48  # thumbnail size for palette extraction — plenty for dominant-color purposes
_PALETTE_COLORS = 8

RGB = Tuple[int, int, int]


def build_theme(image_bytes: bytes, *, dark: bool = True) -> Optional[Theme]:
    """Returns a Theme derived from the image's dominant colors, or None if it can't be decoded."""
    try:
        colors = _dominant_colors(image_bytes)
    except Exception:
        return None
    if not colors:
        return None

    wash_rgb, accent_rgb = _pick_wash_and_accent(colors)

    return Theme(
        name=THEME_NAME,
        primary=_shade(wash_rgb, value=0.62, saturation_scale=0.85),
        accent=_shade(accent_rgb, value=0.78, saturation_scale=1.0),
        background=_shade(wash_rgb, value=0.09, saturation_scale=0.55),
        surface=_shade(wash_rgb, value=0.15, saturation_scale=0.6),
        panel=_shade(wash_rgb, value=0.22, saturation_scale=0.65),
        foreground="#f2f2f2",
        dark=dark,
    )


def _dominant_colors(image_bytes: bytes) -> List[Tuple[RGB, int]]:
    """(rgb, pixel_count) pairs for the image's most common colors, most frequent first."""
    with PILImage.open(io.BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        img.thumbnail((_SAMPLE_SIZE, _SAMPLE_SIZE))
        quantized = img.quantize(colors=_PALETTE_COLORS)

    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    counts.sort(key=lambda c: c[0], reverse=True)

    colors = []
    for count, index in counts:
        offset = index * 3
        rgb = palette[offset : offset + 3]
        if len(rgb) == 3:
            colors.append(((rgb[0], rgb[1], rgb[2]), count))
    return colors


def _pick_wash_and_accent(colors: List[Tuple[RGB, int]]) -> Tuple[RGB, RGB]:
    # The background/surface wash wants an actual hue, not a near-white/black/gray pixel (very
    # common as the single most frequent color in a photo) — fall back to it only if the whole
    # image is basically monochrome.
    hued = [rgb for rgb, _ in colors if _has_hue(rgb)]
    wash_rgb = hued[0] if hued else colors[0][0]

    # Accent wants to *pop*: the most saturated, reasonably bright color among the frequent
    # ones, so buttons/highlights read clearly instead of blending into the wash.
    candidates = [rgb for rgb, _ in colors[:6]] or [c[0] for c in colors]
    accent_rgb = max(candidates, key=_vividness)

    return wash_rgb, accent_rgb


def _has_hue(rgb: RGB) -> bool:
    _, s, v = _rgb_to_hsv(rgb)
    return s >= 0.12 and 0.05 <= v <= 0.95


def _vividness(rgb: RGB) -> float:
    _, s, v = _rgb_to_hsv(rgb)
    return s * min(v, 0.9)


def _rgb_to_hsv(rgb: RGB) -> Tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)


def _shade(rgb: RGB, *, value: float, saturation_scale: float) -> str:
    """Same hue as `rgb`, but at a controlled saturation/brightness — for building a background/
    surface/primary ladder from one dominant color instead of using it verbatim."""
    h, s, _ = _rgb_to_hsv(rgb)
    s = max(0.0, min(1.0, s * saturation_scale))
    r, g, b = colorsys.hsv_to_rgb(h, s, value)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))
