"""Full-screen ambient visuals behind wallpaper mode ("w").

These are proper little pixel shaders rather than character drawings: numpy renders a field at
two pixels per terminal cell, the field is quantized to an album-derived palette, and each pair of
vertical pixels is emitted as a `▀` half-block. That gives a full-bleed, truecolor scene at twice
the vertical resolution of ordinary terminal art.

The expensive part of a Textual frame is not the shader — it is styling thousands of individual
cells. Two things keep that in check:

* the image is quantized to a small palette, then adjacent runs shorter than two cells are merged
  when their colors are close (bright stars and specular highlights are deliberately kept);
* the widget renders prebuilt `Segment` objects directly instead of handing Rich a `Text` with
  thousands of spans. Rich's span application and line splitting is many times slower than
  yielding segments that already fit their rows.

The palette follows the current cover-art accent. Hues are rotated rather than used verbatim:
one accent pixel says very little about a scene, but its hue is enough to tint a sunset, an
aurora, or a pool of water without them all turning into the same color.
"""

from __future__ import annotations

import colorsys
import math
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image
from rich.cells import cell_len, set_cell_size
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.widget import Widget

from .state import AppState, WallpaperVisual

_RGB = np.ndarray
RGB = Tuple[int, int, int]

# 128 entries is enough to keep smooth sky/planet gradients from banding while still leaving the
# run-merge pass with long, cheap stretches to coalesce.
_PALETTE_COLORS = 128
_FRAME_INTERVAL = 0.10  # ~10fps; these are ambient visuals, not a twitch shooter
_MERGE_MIN = 2
_MERGE_DISTANCE = 40.0
_BRIGHT_KEEP = 165.0
_MAX_QUANTIZERS = 8

_FALLBACK_ACCENT = "#ffa62b"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_hex(value: Optional[str]) -> Optional[RGB]:
    if not value or len(value) != 7 or value[0] != "#":
        return None
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError:
        return None


def _hsv(hue: float, saturation: float, value: float) -> _RGB:
    r, g, b = colorsys.hsv_to_rgb(
        hue % 1.0, _clamp(saturation, 0.0, 1.0), _clamp(value, 0.0, 1.0)
    )
    return np.array((r * 255.0, g * 255.0, b * 255.0), dtype=np.float64)


def _mix(
    first: _RGB | Sequence[float],
    second: _RGB | Sequence[float],
    amount: float | np.ndarray,
) -> _RGB:
    a = first if isinstance(first, np.ndarray) else np.asarray(first, dtype=np.float64)
    b = second if isinstance(second, np.ndarray) else np.asarray(second, dtype=np.float64)
    # Most callers blend two RGB triples with a scalar. Avoid numpy's scalar clip and array
    # coercion there; the per-pixel vectors take the array path.
    if np.ndim(amount) == 0:
        amount = min(1.0, max(0.0, float(amount)))
        return a + (b - a) * amount
    amount = np.clip(amount, 0.0, 1.0)
    return a + (b - a) * amount


def _luma(rgb: Sequence[float]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _ramp_rows(stops: Sequence[Tuple[float, _RGB]], count: int) -> _RGB:
    """Sample a list of (position 0..1, color) stops into `count` rows, vectorized."""
    if count <= 0:
        return np.zeros((0, 3), dtype=np.float64)
    positions = np.linspace(0.0, 1.0, count)
    xs = np.array([stop[0] for stop in stops], dtype=np.float64)
    out = np.empty((count, 3), dtype=np.float64)
    for channel in range(3):
        ys = np.array([stop[1][channel] for stop in stops], dtype=np.float64)
        out[:, channel] = np.interp(positions, xs, ys)
    return out


def _smooth(values: _RGB | np.ndarray, radius: int = 2) -> np.ndarray:
    if values.size < 3 or radius <= 0:
        return values
    kernel = np.ones(radius * 2 + 1, dtype=np.float64) / (radius * 2 + 1)
    padded = np.pad(values, radius, mode="edge")
    return np.convolve(padded, kernel, mode="same")[radius:-radius]


def _blur3(image: _RGB, passes: int = 1) -> _RGB:
    """A cheap separable-ish box blur, used only for atmospheric aurora bloom."""
    for _ in range(passes):
        padded = np.pad(image, ((1, 1), (1, 1), (0, 0)), mode="edge")
        image = (
            padded[:-2, :-2]
            + padded[:-2, 1:-1]
            + padded[:-2, 2:]
            + padded[1:-1, :-2]
            + padded[1:-1, 1:-1]
            + padded[1:-1, 2:]
            + padded[2:, :-2]
            + padded[2:, 1:-1]
            + padded[2:, 2:]
        ) / 9.0
    return image


def _vignette(image: _RGB, amount: float = 0.12) -> _RGB:
    height, width = image.shape[:2]
    yy = np.arange(height, dtype=np.float64)[:, None]
    xx = np.arange(width, dtype=np.float64)[None, :]
    vx = (xx / max(1, width) - 0.5) * 2.0
    vy = (yy / max(1, height) - 0.5) * 2.0
    mask = np.clip(1.0 - amount * (vx**2 + vy**2), 0.0, 1.0)
    return np.clip(image * mask[..., None], 0.0, 255.0)


def _to_image(pixels: _RGB) -> Image.Image:
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def _clean_text(value: str) -> str:
    return "".join(
        ch for ch in value if ch >= " " and ch not in ("\x7f", "\u200b", "\ufeff")
    )


def _fit_text(value: str, width: int) -> str:
    value = _clean_text(value)
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    if width == 1:
        return "…"
    return set_cell_size(value, max(0, width - 1)) + "…"


class _Mood:
    """Scene colors derived from the current cover-art accent."""

    def __init__(self, accent: Optional[str]) -> None:
        parsed = _parse_hex(accent) or _parse_hex(_FALLBACK_ACCENT)
        assert parsed is not None
        h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in parsed))
        self.h = h
        # Lift near-black album art so highlights still read on a dark terminal.
        self.accent = _hsv(h, _clamp(s, 0.52, 0.86), max(0.82, v))
        self.hot = _hsv(h + 0.015, 0.46, 1.0)
        self.cream = _mix(self.hot, (255.0, 255.0, 255.0), 0.62)
        self.ember = _hsv(h - 0.025, 0.72, 0.82)
        self.deep = _hsv(h + 0.62, 0.58, 0.028)
        self.mid = _hsv(h + 0.57, 0.58, 0.075)
        self.cool = _hsv(h + 0.50, 0.58, 0.76)
        self.teal = _hsv(h + 0.36, 0.62, 0.82)
        self.violet = _hsv(h + 0.64, 0.56, 0.68)
        self.rose = _hsv(h + 0.93, 0.55, 0.55)


class _Quantizer:
    """A fixed palette for one (visual, accent) pair plus the expensive style cache."""

    def __init__(self, sample: _RGB, colors: int = _PALETTE_COLORS) -> None:
        sample_image = _to_image(sample)
        quantized = sample_image.quantize(
            colors=colors, method=Image.MEDIANCUT, dither=Image.Dither.NONE
        )
        used = quantized.getcolors(maxcolors=colors) or []
        actual = max((index for _, index in used), default=0) + 1
        actual = max(1, min(actual, colors))

        raw_palette = quantized.getpalette() or []
        palette = np.array(
            raw_palette[: actual * 3], dtype=np.uint8
        ).reshape(-1, 3)
        if palette.size == 0:
            palette = np.zeros((1, 3), dtype=np.uint8)

        # quantize(palette=...) looks at all 256 palette slots. Fill the unused slots with the
        # darkest used color rather than black, then clamp any stray index back into the palette.
        darkest = min(range(len(palette)), key=lambda i: _luma(palette[i]))
        full_palette = np.tile(palette[darkest], (256, 1))
        full_palette[: len(palette)] = palette

        self.palette = palette.astype(np.float64)
        self.palette_tuples = [tuple(int(c) for c in color) for color in palette]
        self.size = len(self.palette)
        self._palette_image = Image.new("P", (1, 1))
        self._palette_image.putpalette(full_palette.reshape(-1).tolist())

        pair_indices = np.arange(self.size * self.size)
        fg = self.palette[pair_indices // self.size]
        bg = self.palette[pair_indices % self.size]
        self.pair_luma = (
            0.2126 * fg[:, 0] + 0.7152 * fg[:, 1] + 0.0722 * fg[:, 2]
        )
        self._styles: Dict[int, Style] = {}

    def indices(self, pixels: _RGB) -> np.ndarray:
        image = _to_image(pixels)
        indices = np.asarray(
            image.quantize(palette=self._palette_image, dither=Image.Dither.NONE),
            dtype=np.int32,
        )
        if indices.size and int(indices.max()) >= self.size:
            indices = np.minimum(indices, self.size - 1)
        return indices

    def style(self, pair: int) -> Style:
        style = self._styles.get(pair)
        if style is not None:
            return style
        fg = self.palette[pair // self.size]
        bg = self.palette[pair % self.size]
        style = Style(
            color=Color.from_rgb(int(fg[0]), int(fg[1]), int(fg[2])),
            bgcolor=Color.from_rgb(int(bg[0]), int(bg[1]), int(bg[2])),
        )
        self._styles[pair] = style
        return style

    def pair_close(self, first: int, second: int) -> bool:
        a_fg = self.palette_tuples[first // self.size]
        a_bg = self.palette_tuples[first % self.size]
        b_fg = self.palette_tuples[second // self.size]
        b_bg = self.palette_tuples[second % self.size]
        distance = 0.0
        for i in range(3):
            d0 = a_fg[i] - b_fg[i]
            d1 = a_bg[i] - b_bg[i]
            distance += d0 * d0 + d1 * d1
        return distance < _MERGE_DISTANCE * _MERGE_DISTANCE


class _SegmentRenderable:
    """A Rich renderable that yields prebuilt, already line-sized segments.

    This is the whole performance trick: Rich spends most of a frame applying thousands of Text
    spans and then splitting them at cell boundaries. These segments already fit their rows, so
    the console only has to hand them to Textual's compositor.
    """

    __slots__ = ("segments",)

    def __init__(self, segments: Sequence[Segment]) -> None:
        self.segments = segments

    def __rich_console__(self, console, options):
        yield from self.segments


class WallpaperRenderer:
    """Pure frame generation, separate from the widget so it can be previewed without an app."""

    def __init__(self) -> None:
        self._star_key: Optional[Tuple[int, int]] = None
        self._stars = np.zeros((0, 5), dtype=np.float64)
        self._quantizers: Dict[Tuple[str, str], _Quantizer] = {}

    # -- public ------------------------------------------------------------------------------

    def render_segments(
        self,
        state: AppState,
        cols: int,
        rows: int,
        accent: Optional[str] = None,
        *,
        when: Optional[float] = None,
    ) -> List[Segment]:
        cols, rows = max(1, int(cols)), max(1, int(rows))
        if cols < 2 or rows < 2:
            return []
        visual = state.wallpaper_visual
        now = time.monotonic() if when is None else when
        # The spectrum is the one audio-reactive visual: it holds its whole frame while paused,
        # including the slow shimmer, so a paused track doesn't twitch.
        if (
            visual is WallpaperVisual.SPECTRUM
            and state.now_playing is not None
            and not state.is_playing
        ):
            now = max(0.0, state.position_seconds)

        mood = _Mood(accent)
        pixel_rows = rows * 2
        pixels = self._render_pixels(state, cols, pixel_rows, now, mood, visual)
        quantizer = self._quantizer(visual, accent, pixels)
        indices = quantizer.indices(pixels)
        segments = self._pixel_segments(indices, cols, rows, quantizer)
        self._append_caption(segments, state, cols, rows, mood, visual)
        return segments

    # -- palette / geometry ------------------------------------------------------------------

    def _quantizer(
        self, visual: WallpaperVisual, accent: Optional[str], sample: _RGB
    ) -> _Quantizer:
        key = (visual.value, accent or "")
        quantizer = self._quantizers.get(key)
        if quantizer is None:
            if len(self._quantizers) >= _MAX_QUANTIZERS:
                self._quantizers.pop(next(iter(self._quantizers)))
            quantizer = _Quantizer(sample)
            self._quantizers[key] = quantizer
        return quantizer

    def _ensure_stars(self, width: int, height: int) -> None:
        key = (width, height)
        if self._star_key == key:
            return
        self._star_key = key
        rng = np.random.default_rng(width * 10_007 + height * 31)
        count = max(28, min(170, (width * height) // 180))
        xs = rng.random(count)
        ys = rng.random(count)
        brightness = rng.uniform(0.28, 1.0, count)
        speeds = rng.uniform(0.35, 1.2, count)
        phases = rng.uniform(0.0, math.tau, count)
        self._stars = np.column_stack((xs, ys, brightness, speeds, phases))

    def _draw_stars(
        self, pixels: _RGB, when: float, mood: _Mood, max_y: float = 0.82
    ) -> None:
        height, width = pixels.shape[:2]
        if width < 2 or height < 2:
            return
        self._ensure_stars(width, height)
        count = min(len(self._stars), max(20, (width * height) // 240))
        star_color = _mix(mood.cool, (210.0, 220.0, 245.0), 0.55)
        for i in range(count):
            xn, yn, brightness, speed, phase = self._stars[i]
            if yn >= max_y:
                continue
            x = int(xn * (width - 1))
            y = int(yn * (height - 1))
            twinkle = 0.72 + 0.28 * math.sin(when * (0.45 + speed * 0.55) + phase)
            level = brightness * twinkle
            if level < 0.18:
                continue
            pixels[y, x] = np.clip(pixels[y, x] + star_color * level, 0.0, 255.0)

    # -- scene dispatch ----------------------------------------------------------------------

    def _render_pixels(
        self,
        state: AppState,
        width: int,
        height: int,
        when: float,
        mood: _Mood,
        visual: WallpaperVisual,
    ) -> _RGB:
        if visual is WallpaperVisual.AURORA:
            return self._aurora(state, width, height, when, mood)
        if visual is WallpaperVisual.RIPPLE:
            return self._ripple(state, width, height, when, mood)
        if visual is WallpaperVisual.ORBIT:
            return self._orbit(state, width, height, when, mood)
        return self._spectrum(state, width, height, when, mood)

    # -- spectrum ----------------------------------------------------------------------------

    def _spectrum(
        self, state: AppState, width: int, height: int, when: float, mood: _Mood
    ) -> _RGB:
        """A neon sunset over water: the track's real spectrum becomes the mountain skyline,
        mirrored into a rippling reflection."""
        horizon = max(2, min(height - 2, int(height * 0.62)))
        if height < 8:
            horizon = max(1, height - 1)

        yy = np.arange(height, dtype=np.float64)[:, None]
        xx = np.arange(width, dtype=np.float64)[None, :]

        sky_stops = (
            (0.0, _mix(mood.deep, (2.0, 3.0, 8.0), 0.35)),
            (0.42, _mix(mood.deep, mood.mid, 0.75)),
            (0.70, mood.violet),
            (0.86, _mix(mood.rose, mood.ember, 0.30)),
            (0.96, mood.ember),
            (1.0, _mix(mood.hot, mood.cream, 0.30)),
        )
        sky = _ramp_rows(sky_stops, horizon)
        pixels = np.empty((height, width, 3), dtype=np.float64)
        pixels[:horizon] = sky[:, None, :]

        water_stops = (
            (0.0, _mix(mood.rose, mood.deep, 0.72)),
            (0.34, _mix(mood.mid, mood.deep, 0.62)),
            (1.0, _mix(mood.deep, (1.0, 2.0, 6.0), 0.30)),
        )
        pixels[horizon:] = _ramp_rows(water_stops, height - horizon)[:, None, :]

        horizon_glow = np.exp(
            -(((yy - (horizon - 1.0)) / max(1.5, height * 0.045)) ** 2)
        )
        pixels += horizon_glow[..., None] * _mix(mood.ember, (255.0, 140.0, 70.0), 0.22) * 0.23

        self._draw_stars(pixels, when, mood, max_y=0.72)

        # Sun. A circle in pixel space (terminal half-block pixels are roughly square).
        sun_cx = width * 0.5
        sun_cy = horizon - height * 0.215
        sun_r = max(2.0, min(width * 0.11, height * 0.185))
        distance = np.sqrt(
            ((xx - sun_cx) / max(0.5, sun_r * 1.04)) ** 2
            + ((yy - sun_cy) / max(0.5, sun_r)) ** 2
        )
        rim = _mix(mood.ember, (190.0, 58.0, 58.0), 0.50)
        sun_color = _mix(
            rim[None, None, :],
            mood.hot[None, None, :],
            np.clip((1.0 - distance) / 0.75, 0.0, 1.0)[..., None] ** 1.2,
        )
        sun_color = _mix(
            sun_color,
            mood.cream[None, None, :],
            np.clip((0.55 - distance) / 0.55, 0.0, 1.0)[..., None] ** 2.0,
        )
        disc = distance <= 1.0
        cycle = max(3, int(sun_r * 0.30))
        stripe = ((yy - (sun_cy - sun_r)) % cycle) < max(1, cycle * 0.28)
        stripe &= yy > sun_cy - sun_r * 0.05
        disc &= ~stripe
        pixels[disc] = sun_color[disc]

        halo = np.exp(-np.clip(distance - 1.0, 0.0, None) * 3.0) * (distance > 1.0)
        halo += 0.24 * np.exp(-np.clip(distance - 1.0, 0.0, None) * 8.0)
        halo = np.clip(halo, 0.0, 1.0)
        pixels += halo[..., None] * mood.hot * 0.34

        # Spectrum ridge.
        levels = list(state.visualizer_levels)
        if state.now_playing is not None and len(levels) >= 2:
            bars = np.asarray(levels, dtype=np.float64) / 100.0
        else:
            phases = np.arange(24, dtype=np.float64)
            bars = 0.22 + 0.16 * np.sin(when * 0.55 + phases * 0.45)
            bars += 0.07 * np.sin(when * 0.23 - phases * 0.19)
        bars = np.clip(bars, 0.0, 1.0)
        bar_x = np.linspace(0.0, len(bars) - 1.0, width)
        lower = np.floor(bar_x).astype(int)
        upper = np.minimum(lower + 1, len(bars) - 1)
        frac = bar_x - lower
        frac = frac * frac * (3.0 - 2.0 * frac)
        ridge = _smooth(bars[lower] * (1.0 - frac) + bars[upper] * frac, radius=2)
        ridge = np.clip(ridge, 0.0, 1.0)

        # Far ridge, then the dark main silhouette.
        back_ridge = 0.17 + 0.09 * np.sin(
            np.linspace(0.0, math.tau * 1.8, width) + when * 0.18
        )
        back_ridge = np.clip(back_ridge + _smooth(ridge, 3) * 0.28, 0.0, 0.70)
        back_peak = horizon - 1.0 - back_ridge * (horizon * 0.40)
        back_mask = (yy >= np.ceil(back_peak)[None, :]) & (yy < horizon)
        back_color = _mix(mood.violet, _mix(mood.deep, mood.rose, 0.18), 0.55)
        pixels[back_mask] = back_color

        main_height = (0.04 + 0.96 * ridge) * horizon * 0.48
        main_top = horizon - main_height
        main_mask = yy >= np.ceil(main_top)[None, :]
        for y in range(horizon):
            depth = (horizon - y) / max(1.0, horizon)
            color = _mix(
                mood.deep,
                _mix(mood.violet, mood.rose, 0.25),
                _clamp(0.55 - depth * 0.9, 0.0, 0.55),
            )
            pixels[y, main_mask[y]] = color

        rim_color = _mix(mood.hot, mood.cream, 0.15)
        xs = np.arange(width)
        top = np.floor(main_top).astype(np.int64)
        frac = main_top - top
        alpha = 1.0 - frac
        valid = (top >= 0) & (top < horizon)
        pixels[top[valid], xs[valid]] = _mix(
            pixels[top[valid], xs[valid]],
            rim_color,
            (0.90 * alpha[valid])[:, None],
        )
        for delta in range(1, 4):
            ys = top + delta
            mask = (top >= 0) & (ys < horizon)
            if np.any(mask):
                pixels[ys[mask], xs[mask]] = _mix(
                    pixels[ys[mask], xs[mask]],
                    rim_color,
                    0.32 * math.exp(-delta / 1.35),
                )
        above = top - 1
        mask = above >= 0
        if np.any(mask):
            pixels[above[mask], xs[mask]] = _mix(
                pixels[above[mask], xs[mask]], rim_color, 0.20
            )

        # Water reflection.
        for y in range(horizon, height):
            depth = y - horizon
            shift = int(round(math.sin(y * 0.62 + when * 1.9) * 2.0))
            source_x = np.clip(np.arange(width) + shift, 0, width - 1)
            reflected_height = main_height[source_x] * 0.82
            mask = depth < reflected_height
            if np.any(mask):
                fade = np.clip(1.0 - depth / np.maximum(reflected_height, 1.0), 0.0, 1.0)
                color = _mix(
                    _mix(mood.deep, mood.rose, 0.10),
                    mood.deep,
                    (0.35 + 0.4 * (1.0 - fade))[:, None],
                )
                pixels[y, mask] = _mix(pixels[y, mask], color[mask], 0.55)
            near_rim = np.abs(depth - reflected_height) < 1.2
            if np.any(near_rim):
                pixels[y, near_rim] = _mix(
                    pixels[y, near_rim], _mix(mood.ember, mood.deep, 0.35), 0.28
                )
            sheen = (
                np.sin(xx[0] * 0.22 + y * 1.1 + when * 1.7) * 0.5 + 0.5
            ) ** 8
            sheen *= math.exp(-depth / max(4.0, height * 0.24))
            pixels[y] += sheen[:, None] * _mix(mood.ember, (70.0, 25.0, 10.0), 0.35) * 0.16

        # Broken sun column on the water.
        for y in range(horizon, height):
            depth = y - horizon
            column_width = (
                sun_r * 0.60 * (1.0 - depth / max(1.0, height - horizon)) + 1.2
            )
            mask = np.abs(xx[0] - sun_cx) < column_width
            if not np.any(mask):
                continue
            shimmer = (
                np.sin(y * 1.28 - when * 2.6 + xx[0] * 0.31) * 0.5 + 0.5
            ) ** 3
            strength = (1.0 - depth / max(1.0, height - horizon)) ** 1.5
            pixels[y, mask] += (
                shimmer[mask] * strength
            )[:, None] * _mix(mood.hot, mood.ember, 0.35) * 0.46

        return _vignette(pixels, 0.10)

    # -- aurora ------------------------------------------------------------------------------

    def _aurora(
        self, state: AppState, width: int, height: int, when: float, mood: _Mood
    ) -> _RGB:
        """Hanging curtains of light over a dark ridge.

        Each curtain is a sheet whose lower edge waves across the screen; broad gaussian beams
        inside it are the vertical rays. The beams are deliberately soft and overlapping — a hard
        sine comb reads as a picket fence at terminal resolution.
        """
        yy = np.arange(height, dtype=np.float64)[:, None]
        xx = np.arange(width, dtype=np.float64)[None, :]
        xn = xx / max(1.0, width)
        yn = yy / max(1.0, height)

        sky_stops = (
            (0.0, _mix(mood.deep, (1.0, 2.0, 6.0), 0.55)),
            (0.45, _mix(mood.deep, mood.mid, 0.62)),
            (0.78, _mix(mood.mid, mood.teal, 0.06)),
            (1.0, _mix(mood.deep, mood.teal, 0.18)),
        )
        pixels = np.repeat(_ramp_rows(sky_stops, height)[:, None, :], width, axis=1)
        cloud = (np.sin(xx * 0.025 + 1.2) * 0.5 + 0.5) * (
            np.sin(yy * 0.020 - 0.4) * 0.5 + 0.5
        )
        pixels += cloud[..., None] * np.array((6.0, 9.0, 20.0))

        self._draw_stars(pixels, when, mood, max_y=0.80)

        glow = np.zeros((height, width, 3), dtype=np.float64)
        curtains = (
            (0.06, 0.58, 0.09, 13, 0.88, 0.76, 0.40),
            (0.01, 0.33, 0.060, 9, 0.50, 0.90, 0.68),
        )
        accent_shift = (mood.h - 0.09) * 0.06
        for ci, (top0, hem0, wave, count, brightness, hue_top, hue_hem) in enumerate(
            curtains
        ):
            top = top0 + 0.022 * np.sin(
                xn * math.tau * 1.1 + when * 0.17 + ci * 2.0
            )
            top += 0.012 * np.sin(xn * math.tau * 3.4 - when * 0.28)
            hem = hem0 + wave * np.sin(
                xn * math.tau * 0.9 + when * (0.16 + ci * 0.03) + ci * 2.1
            )
            hem += wave * 0.34 * np.sin(xn * math.tau * 2.6 - when * 0.25)
            span = np.maximum(hem - top, 0.04)
            v = (yn - top) / span

            profile = np.exp(-(((1.0 - v) * 1.20) ** 2))
            profile *= np.exp(-(np.clip(v - 1.0, 0.0, None) ** 2) * 6.0)
            profile = np.clip(profile, 0.0, 1.2)

            rays = np.zeros((height, width), dtype=np.float64)
            for j in range(count):
                phase = when * (0.045 + 0.012 * ci) + j * 2.399963 + ci * 1.3
                center = 0.5 + 0.47 * math.sin(phase * 0.83 + j * 1.7)
                center = center + 0.028 * np.sin(v * 3.0 + when * 0.25 + j)
                beam_width = 0.030 + 0.030 * (
                    0.5 + 0.5 * math.sin(j * 1.3 + when * 0.12)
                )
                amplitude = 0.45 + 0.45 * (
                    0.5 + 0.5 * math.sin(j * 2.1 + when * 0.09)
                )
                rays += amplitude * np.exp(-(((xn - center) / beam_width) ** 2))
            rays = np.clip(rays, 0.0, 1.5)

            fine = 0.86 + 0.14 * (
                0.5
                + 0.5
                * np.sin(
                    xn * math.tau * 43
                    + 2.0 * np.sin(xn * math.tau * 2.0 + when * 0.2)
                    + when * 0.8
                )
            )
            envelope = 0.60 + 0.40 * (
                0.5
                + 0.5
                * np.sin(xn * math.tau * 1.25 + when * 0.15 + ci * 1.8)
            )
            diffuse = 0.12 * profile
            intensity = (
                profile * (0.18 + 0.82 * rays) * fine * envelope + diffuse
            ) * brightness
            hemline = (
                np.exp(-(((yn - (hem - 0.004)) / 0.020) ** 2))
                * brightness
                * (0.025 + 0.15 * rays)
                * envelope
            )
            intensity = np.clip(intensity + hemline, 0.0, 1.38)

            vv = np.clip(v, 0.0, 1.2)[..., None]
            color_top = _hsv(
                hue_top + accent_shift, 0.55, 0.85
            )
            color_hem = _hsv(hue_hem + accent_shift, 0.64, 1.0)
            color = color_top * (1.0 - np.clip(vv, 0.0, 1.0)) + color_hem * np.clip(
                vv, 0.0, 1.0
            )
            core = _mix(color_hem, mood.cream, 0.45)
            glow += color * (intensity**1.55)[..., None] * 1.0
            glow += core[None, None, :] * (
                np.clip(intensity - 1.02, 0.0, 1.0)[..., None] ** 1.5
            ) * 0.32

        glow += _blur3(glow, 1) * 0.24
        # Screen blend keeps the dark sky and stars underneath.
        src = np.clip(glow / 255.0, 0.0, 1.0)
        pixels = 255.0 - (255.0 - pixels) * (1.0 - src)

        # Distant ridge silhouette. A little height variation keeps it from reading as a border.
        ridge = 0.040 + 0.050 * (
            0.5 + 0.5 * np.sin(xx * 0.019 + 1.3)
        ) + 0.022 * (0.5 + 0.5 * np.sin(xx * 0.043 - 2.1))
        ridge_y = height - np.ceil(ridge * height).astype(int)
        mask = yy >= ridge_y
        dark = _mix(mood.deep, (1.0, 2.0, 5.0), 0.55)
        pixels[mask] = _mix(pixels[mask], dark, 0.94)
        # A barely-there cold catch on the crest.
        for x in range(width):
            y = int(ridge_y[0, x])
            if 0 <= y < height:
                pixels[y, x] = _mix(
                    pixels[y, x], _mix(mood.teal, mood.cream, 0.18), 0.12
                )

        return _vignette(pixels, 0.16)

    # -- ripple ------------------------------------------------------------------------------

    def _ripple(
        self, state: AppState, width: int, height: int, when: float, mood: _Mood
    ) -> _RGB:
        """Luminous caustics on deep water: three slow interfering sources, sharpened into thin
        contour lines. Unlike the other scenes this is almost architectural — the etched lines
        are the point."""
        yy = np.arange(height, dtype=np.float64)[:, None]
        xx = np.arange(width, dtype=np.float64)[None, :]

        water_stops = (
            (0.0, _mix(mood.deep, (1.0, 2.0, 7.0), 0.45)),
            (0.45, _mix(mood.deep, mood.mid, 0.55)),
            (1.0, _mix(mood.mid, mood.teal, 0.20)),
        )
        pixels = np.repeat(_ramp_rows(water_stops, height)[:, None, :], width, axis=1)
        pixels += (
            np.sin(xx * 0.032 + 0.8) * 0.5 + 0.5
        )[..., None] * np.array((3.0, 6.0, 10.0))

        sources = []
        for i in range(3):
            phase = when * (0.050 + i * 0.011) + i * 2.3
            cx = width * (0.5 + 0.30 * math.sin(phase * 0.83 + i * 1.7))
            cy = height * (0.48 + 0.26 * math.cos(phase * 0.57 + i * 2.1))
            sources.append((cx, cy, when * (0.95 + i * 0.19) + i * 1.9))

        field = np.zeros((height, width), dtype=np.float64)
        norm = np.zeros((height, width), dtype=np.float64)
        for i, (cx, cy, speed) in enumerate(sources):
            dx = (xx - cx) * 0.55
            dy = yy - cy
            distance = np.sqrt(dx * dx + dy * dy)
            envelope = np.exp(-distance / max(8.0, height * 0.46))
            field += (0.85 if i == 0 else 0.6) * envelope * np.sin(
                distance * 0.52 - speed * 1.25 + i * 2.4
            )
            norm += envelope
        field += 0.20 * np.sin(xx * 0.045 + yy * 0.060 + when * 0.30)
        field += 0.10 * np.sin(xx * 0.018 - yy * 0.042 - when * 0.18)
        norm += 0.32
        field /= np.maximum(norm, 0.01)

        caustic = np.clip(0.5 + 0.5 * field, 0.0, 1.0) ** 3.0
        caustic = caustic**1.25
        pixels = _mix(
            pixels, mood.teal[None, None, :], (0.10 + 0.55 * caustic**0.7)[..., None]
        )
        pixels += caustic[..., None] ** 3 * np.array((70.0, 55.0, 40.0))

        gy, gx = np.gradient(field)
        grad = np.maximum(np.abs(gx), np.abs(gy))
        for level, strength in ((0.0, 0.82), (0.72, 0.28), (-0.72, 0.20)):
            line = np.abs(field - level) < 0.55 * grad
            line_color = _mix(
                mood.teal,
                mood.cream,
                0.55 if strength > 0.4 else 0.30,
            )
            pixels[line] = _mix(pixels[line], line_color, strength)

        # Deterministic sparkle instead of random(), so glints travel rather than flicker.
        sparkle_hash = np.sin(xx * 12.9898 + yy * 78.233 + when * 2.0) * 43758.5453
        sparkle_hash = sparkle_hash - np.floor(sparkle_hash)
        sparkle = (caustic > 0.82) & (sparkle_hash < 0.014)
        pixels[sparkle] = np.clip(
            pixels[sparkle] + _mix(mood.cream, mood.hot, 0.25) * 0.55,
            0.0,
            255.0,
        )

        return _vignette(pixels, 0.16)

    # -- orbit -------------------------------------------------------------------------------

    def _orbit(
        self, state: AppState, width: int, height: int, when: float, mood: _Mood
    ) -> _RGB:
        """A ringed planet turning in deep space.

        The planet is a lit sphere — a normal from the disk gives the terminator, a broad rim
        gives the atmosphere, and a pair of longitude/latitude waves put slow cloud bands across
        the day side. The rings are an elliptical distance field with gaps and a bright ice edge;
        their near half is drawn over the planet so the disk really sits inside the system.
        """
        yy = np.arange(height, dtype=np.float64)[:, None]
        xx = np.arange(width, dtype=np.float64)[None, :]

        backdrop_stops = (
            (0.0, _mix(mood.deep, (1.0, 1.5, 5.0), 0.62)),
            (0.55, _mix(mood.deep, mood.mid, 0.40)),
            (1.0, _mix(mood.deep, mood.violet, 0.16)),
        )
        pixels = np.repeat(
            _ramp_rows(backdrop_stops, height)[:, None, :], width, axis=1
        )
        nebula = (np.sin(xx * 0.020 + 1.7) * 0.5 + 0.5) * (
            np.sin(yy * 0.016 - 0.9) * 0.5 + 0.5
        )
        nebula += 0.55 * (np.sin(xx * 0.047 - 1.1 + when * 0.02) * 0.5 + 0.5) * (
            np.sin(yy * 0.036 + 2.2 - when * 0.015) * 0.5 + 0.5
        )
        pixels += nebula[..., None] * _mix(mood.violet, mood.teal, 0.55) * 0.055

        self._draw_stars(pixels, when, mood, max_y=1.0)

        # Planet disk and its local normal.
        cx, cy = width * 0.54, height * 0.49
        radius = max(4.0, min(width * 0.215, height * 0.34))
        dx = (xx - cx) / radius
        dy = (yy - cy) / radius
        disk = dx * dx + dy * dy
        inside = disk <= 1.0
        nz = np.sqrt(np.clip(1.0 - disk, 0.0, 1.0))
        nx, ny = dx, dy
        light = np.array((-0.60, -0.45, 0.66))
        light = light / np.linalg.norm(light)
        lambert = np.clip(
            nx * light[0] + ny * light[1] + nz * light[2], 0.0, 1.0
        )
        outside = disk > 1.0
        halo = np.exp(
            -np.clip(np.sqrt(np.maximum(disk, 1.0)) - 1.0, 0.0, None) * 7.0
        )
        pixels += (
            halo * outside
        )[..., None] * _mix(mood.teal, mood.cream, 0.40) * 0.18

        lon = np.arctan2(nx, nz + 0.001)
        lat = dy
        bands = 0.5 + 0.5 * np.sin(
            lat * 11.0 + 0.7 * np.sin(lat * 4.0 + when * 0.09) + 1.2
        )
        swirl = 0.5 + 0.5 * np.sin(
            lon * 2.4
            + lat * 2.8
            + 1.1 * np.sin(lat * 6.0 - when * 0.12)
            + when * 0.06
        )
        day_base = _mix(mood.accent, mood.hot, 0.58)
        day = day_base[None, None, :] + (
            mood.cream - day_base
        )[None, None, :] * (np.clip(lambert - 0.75, 0.0, 1.0) * 1.4)[..., None]
        night = _mix(mood.deep, mood.violet, 0.38)
        lit = np.clip(lambert**1.05, 0.0, 1.0)[..., None]
        color = night + (day - night) * lit
        color = _mix(
            color,
            _mix(mood.violet, mood.accent, 0.35)[None, None, :],
            (bands * swirl * 0.20)[..., None],
        )
        rim = (1.0 - nz) ** 2.8
        color += (
            (rim * (0.22 + 0.78 * lambert))[..., None]
            * _mix(mood.teal, mood.cream, 0.45)
            * 0.68
        )
        color += (lambert**22)[..., None] * _mix(
            mood.cream, (255.0, 245.0, 225.0), 0.50
        ) * 0.35
        pixels[inside] = color[inside]

        # Rings: an elliptical distance field, tilted around the planet.
        angle = math.radians(-21.0)
        ca, sa = math.cos(angle), math.sin(angle)
        u = (xx - cx) * ca + (yy - cy) * sa
        v = -(xx - cx) * sa + (yy - cy) * ca
        ring_rx = radius * 2.30
        ring_ry = radius * 0.30
        ring_e = np.sqrt((u / ring_rx) ** 2 + (v / ring_ry) ** 2)
        edge = np.clip((ring_e - 1.03) / 0.06, 0.0, 1.0) * np.clip(
            (2.10 - ring_e) / 0.12, 0.0, 1.0
        )
        density = (
            0.46 + 0.54 * (0.5 + 0.5 * np.sin(ring_e * 14.0 + when * 0.22))
        ) * edge
        density *= 1.0 - 0.88 * np.exp(-(((ring_e - 1.47) / 0.040) ** 2))
        density *= 1.0 - 0.65 * np.exp(-(((ring_e - 1.83) / 0.035) ** 2))
        density *= 0.90 + 0.10 * (
            0.5 + 0.5 * np.sin(ring_e * 55.0 + when * 0.50)
        )
        ring_base = _mix(mood.cool, mood.cream, 0.45)
        ring_warm = _mix(mood.accent, mood.cream, 0.35)
        ring_color = ring_base[None, None, :] + (
            ring_warm - ring_base
        )[None, None, :] * (np.clip(1.55 - ring_e, 0.0, 0.5) * 1.1)[..., None]
        ring_light = 0.72 + 0.50 * np.clip(
            (u / ring_rx) * -0.35 + (v / ring_ry) * -0.55 + 0.65, 0.0, 1.0
        )
        ring_rgb = ring_color * ring_light[..., None]
        alpha = np.clip(density * 0.82, 0.0, 1.0)

        back = (ring_e > 1.03) & (ring_e < 2.10) & (v < 0.0)
        pixels[back] = _mix(
            pixels[back], ring_rgb[back], alpha[back][..., None]
        )
        # Re-assert the planet over the back rings, then draw the front half over it. The
        # rings are bright on both sides; a hard cast shadow at this scale reads as a belt
        # strapped across the planet rather than a ring plane.
        pixels[inside] = color[inside]
        front = (ring_e > 1.03) & (ring_e < 2.10) & (v >= 0.0)
        pixels[front] = _mix(
            pixels[front], ring_rgb[front], alpha[front][..., None]
        )

        # A small moon on the upper right.
        mx, my = width * 0.79, height * 0.22
        moon_r = max(2.4, radius * 0.15)
        mdx = (xx - mx) / moon_r
        mdy = (yy - my) / moon_r
        moon_disk = mdx * mdx + mdy * mdy
        moon_inside = moon_disk <= 1.0
        moon_nz = np.sqrt(np.clip(1.0 - moon_disk, 0.0, 1.0))
        moon_lit = np.clip(
            mdx * light[0] + mdy * light[1] + moon_nz * light[2], 0.0, 1.0
        )
        moon_dark = _mix(mood.deep, (90.0, 85.0, 95.0), 0.45)
        moon_bright = _mix(mood.cream, mood.teal, 0.35)
        moon = moon_dark + (moon_bright - moon_dark) * (moon_lit**0.9)[..., None]
        pixels[moon_inside] = moon[moon_inside]

        return _vignette(pixels, 0.11)

    # -- text / caption ----------------------------------------------------------------------

    def _pixel_segments(
        self, indices: np.ndarray, cols: int, rows: int, quantizer: _Quantizer
    ) -> List[Segment]:
        top = indices[0::2]
        bottom = indices[1::2]
        pairs = top.astype(np.int32) * quantizer.size + bottom
        palette = quantizer.palette_tuples
        luma = quantizer.pair_luma
        segments: List[Segment] = []

        for y in range(rows):
            if y:
                segments.append(Segment("\n"))
            row = pairs[y]
            changes = np.flatnonzero(row[1:] != row[:-1]) + 1
            starts = np.concatenate((np.array([0], dtype=np.int64), changes))
            ends = np.concatenate((changes, np.array([cols], dtype=np.int64)))

            out_starts: List[int] = []
            out_ends: List[int] = []
            out_pairs: List[int] = []
            for start, end, pair in zip(starts, ends, row[starts]):
                start_i, end_i, pair_i = int(start), int(end), int(pair)
                if end_i - start_i < _MERGE_MIN and out_pairs:
                    previous = out_pairs[-1]
                    if luma[pair_i] < _BRIGHT_KEEP and quantizer.pair_close(
                        previous, pair_i
                    ):
                        out_ends[-1] = end_i
                        continue
                out_starts.append(start_i)
                out_ends.append(end_i)
                out_pairs.append(pair_i)

            for start, end, pair in zip(out_starts, out_ends, out_pairs):
                segments.append(
                    Segment("▀" * (end - start), quantizer.style(pair))
                )

        return segments

    def _append_caption(
        self,
        segments: List[Segment],
        state: AppState,
        cols: int,
        rows: int,
        mood: _Mood,
        visual: WallpaperVisual,
    ) -> None:
        if rows < 5:
            return
        compact = rows < 9
        # Compact terminals get the title and the control hint; the progress row is the first
        # thing to go when there are only a handful of rows to work with.
        caption_rows = 2 if compact else 4
        caption_rows = min(caption_rows, rows)
        start = rows - caption_rows

        scrim = _mix(mood.deep, (4.0, 5.0, 9.0), 0.55)
        accent_fg = _mix(mood.hot, (255.0, 255.0, 255.0), 0.42)
        hint_fg = _mix(mood.cool, (150.0, 156.0, 170.0), 0.58)
        progress_fg = _mix(mood.hot, mood.cream, 0.35)

        backgrounds: List[_RGB] = []
        for i in range(caption_rows):
            fade = i / max(1, caption_rows - 1)
            backgrounds.append(_mix(scrim, (7.0, 8.0, 13.0), fade))

        # Replace the bottom of the pixel frame with the caption. The pixel rows are already
        # separated by explicit newline segments, so keep the first `start` rows and then append
        # the caption rows; rebuilding the list this way is much cheaper than restyling cells.
        _truncate_lines(segments, start)
        if segments:
            segments.append(Segment("\n"))

        title = (
            f"♪  {state.now_playing.title} — {state.now_playing.artist}"
            if state.now_playing is not None
            else "Wallpaper mode  ·  nothing playing"
        )

        def bg_style(color: _RGB) -> Style:
            return Style(bgcolor=Color.from_rgb(*_ints(color)))

        def fg_style(color: _RGB, background: _RGB, bold: bool = False) -> Style:
            return Style(
                color=Color.from_rgb(*_ints(color)),
                bgcolor=Color.from_rgb(*_ints(background)),
                bold=bold,
            )

        if compact:
            title_bg = backgrounds[0]
            title_style = fg_style(accent_fg, title_bg, bold=True)
            segments.extend(
                _caption_chunks(cols, ((title, title_style),), bg_style(title_bg))
            )
            segments.append(Segment("\n"))
            hint = (
                f"w exit  ·  ←/→ {visual.value}  ·  "
                f"space {'pause' if state.is_playing else 'play'}"
            )
            hint_bg = backgrounds[-1]
            hint_style = fg_style(hint_fg, hint_bg)
            segments.extend(
                _caption_chunks(cols, ((hint, hint_style),), bg_style(hint_bg))
            )
            return

        # Blank breathing row at the top of the scrim.
        segments.extend(_caption_chunks(cols, (), bg_style(backgrounds[0])))
        segments.append(Segment("\n"))

        title_bg = backgrounds[1]
        title_style = fg_style(accent_fg, title_bg, bold=True)
        segments.extend(
            _caption_chunks(cols, ((title, title_style),), bg_style(title_bg))
        )
        segments.append(Segment("\n"))

        # Progress row. Keep the bar and the two times as separate chunks so the bar can be
        # coloured independently without measuring the line twice.
        progress_bg = backgrounds[2]
        progress_style = fg_style(progress_fg, progress_bg)
        progress_hint = fg_style(hint_fg, progress_bg)
        ratio = (
            _clamp(state.progress_ratio, 0.0, 1.0)
            if state.duration_seconds > 0
            else 0.0
        )
        bar_width = min(44, max(8, cols - 24))
        filled = min(bar_width, max(0, int(round(ratio * bar_width))))
        bar = "━" * filled
        if filled < bar_width:
            bar += "╸" + "─" * max(0, bar_width - filled - 1)
        prefix = f"{_format_seconds(state.position_seconds)}  "
        suffix = f"  {_format_seconds(state.duration_seconds)}"
        segments.extend(
            _caption_chunks(
                cols,
                (
                    (prefix, progress_hint),
                    (bar, progress_style),
                    (suffix, progress_hint),
                ),
                bg_style(progress_bg),
            )
        )
        segments.append(Segment("\n"))

        hint_bg = backgrounds[-1]
        hint = (
            f"w exit  ·  ←/→ visual: {visual.value}  ·  "
            f"space {'pause' if state.is_playing else 'play'}  ·  n/p skip"
        )
        hint_style = fg_style(hint_fg, hint_bg)
        segments.extend(
            _caption_chunks(cols, ((hint, hint_style),), bg_style(hint_bg))
        )


class WallpaperPanel(Widget):
    """The full-screen wallpaper surface.

    Playback itself is untouched — AudioPlayerService keeps going regardless of what's on screen;
    this swaps the whole UI for one of the procedural scenes and repaints it on the player's tick.
    """

    DEFAULT_CSS = """
    WallpaperPanel {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self._renderer = WallpaperRenderer()
        self._segments: List[Segment] = []
        self._renderable = _SegmentRenderable(self._segments)
        self._geometry = (0, 0)
        self._accent: Optional[str] = None
        self._visual: Optional[WallpaperVisual] = None
        self._track_id: Optional[str] = None
        self._is_playing: Optional[bool] = None
        self._last_frame = 0.0
        self._static_key: Optional[Tuple] = None

    def on_resize(self, event: events.Resize) -> None:
        if self.state.is_wallpaper_mode:
            self._render_now(self._accent, force=True)

    def _static_signature(self) -> Optional[Tuple]:
        """A content key for the one scene that is genuinely static while paused.

        Repainting a frozen spectrum at 10fps is wasted work: the shader, palette pass, and
        segment builder would all produce the exact same frame. The caption is part of the
        signature, so seeks/title changes still repaint.
        """
        s = self.state
        if (
            s.wallpaper_visual is WallpaperVisual.SPECTRUM
            and s.now_playing is not None
            and not s.is_playing
        ):
            return (
                s.now_playing.id,
                s.now_playing.title,
                s.now_playing.artist,
                round(float(s.position_seconds), 3),
                round(float(s.duration_seconds), 3),
                tuple(s.visualizer_levels),
            )
        return None

    def refresh_content(self, accent: Optional[str] = None) -> None:
        if not self.state.is_wallpaper_mode:
            return
        now = time.monotonic()
        cols, rows = int(self.size.width), int(self.size.height)
        force = (
            (cols, rows) != self._geometry
            or accent != self._accent
            or self.state.wallpaper_visual != self._visual
            or self.state.is_playing != self._is_playing
            or (
                self.state.now_playing is not None
                and self.state.now_playing.id != self._track_id
            )
        )
        static_key = self._static_signature()
        if not force and static_key is not None and static_key == self._static_key:
            return
        if not force and now - self._last_frame < _FRAME_INTERVAL:
            return
        self._render_now(accent, force=force)

    def _render_now(self, accent: Optional[str], *, force: bool = False) -> None:
        cols, rows = int(self.size.width), int(self.size.height)
        if cols < 2 or rows < 2:
            return
        if not force and (cols, rows) == self._geometry and self._segments:
            if time.monotonic() - self._last_frame < _FRAME_INTERVAL:
                return
        self._segments = self._renderer.render_segments(
            self.state, cols, rows, accent
        )
        self._renderable = _SegmentRenderable(self._segments)
        self._geometry = (cols, rows)
        self._accent = accent
        self._visual = self.state.wallpaper_visual
        self._track_id = (
            self.state.now_playing.id if self.state.now_playing is not None else None
        )
        self._is_playing = self.state.is_playing
        self._last_frame = time.monotonic()
        self._static_key = self._static_signature()
        self.refresh()

    def render(self):
        if not self.state.is_wallpaper_mode:
            return self._renderable
        if not self._segments:
            cols, rows = int(self.size.width), int(self.size.height)
            if cols >= 2 and rows >= 2:
                self._segments = self._renderer.render_segments(
                    self.state, cols, rows, self._accent
                )
                self._renderable = _SegmentRenderable(self._segments)
                self._geometry = (cols, rows)
                self._visual = self.state.wallpaper_visual
                self._track_id = (
                    self.state.now_playing.id
                    if self.state.now_playing is not None
                    else None
                )
                self._is_playing = self.state.is_playing
                self._last_frame = time.monotonic()
                self._static_key = self._static_signature()
        return self._renderable


def _ints(rgb: _RGB | Sequence[float]) -> Tuple[int, int, int]:
    return tuple(int(_clamp(float(c), 0.0, 255.0)) for c in rgb[:3])


def _format_seconds(total_seconds: float) -> str:
    total = max(0, int(total_seconds))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _caption_chunks(
    cols: int,
    chunks: Sequence[Tuple[str, Style]],
    base: Style,
) -> List[Segment]:
    """Lay out centered styled chunks and pad them to exactly `cols` cells."""
    cols = max(1, cols)
    content = [(text, style) for text, style in chunks if text]
    total = sum(cell_len(text) for text, _ in content)
    if total <= cols:
        left = (cols - total) // 2
        right = cols - total - left
    else:
        left = right = 0

    out: List[Segment] = []
    if left:
        out.append(Segment(" " * left, base))
    remaining = cols - left - right
    for text, style in content:
        if remaining <= 0:
            break
        fitted = _fit_text(text, remaining)
        if not fitted:
            continue
        width = cell_len(fitted)
        out.append(Segment(fitted, style))
        remaining -= width
    used = sum(cell_len(segment.text) for segment in out)
    if used < cols:
        out.append(Segment(" " * (cols - used), base))
    return out


def _truncate_lines(segments: List[Segment], keep: int) -> None:
    """Keep the first `keep` rows of a segment list and drop the rest."""
    if keep <= 0:
        segments.clear()
        return
    newlines = 0
    cut = len(segments)
    for index, segment in enumerate(segments):
        if segment.text == "\n":
            newlines += 1
            if newlines >= keep:
                cut = index
                break
    del segments[cut:]
    while segments and segments[-1].text == "\n":
        segments.pop()
