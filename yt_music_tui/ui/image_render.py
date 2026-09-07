from __future__ import annotations

import numpy as np
from rich.text import Text

"""
Renders an RGB pixel grid as terminal "half-block" ANSI art: each character cell shows two
pixel rows at once (▀, foreground = top pixel, background = bottom pixel), which needs only
truecolor support — no sixel/kitty graphics protocol, and no extra terminal-specific deps.
"""

_UPPER_HALF_BLOCK = "▀"


def render_half_blocks(pixels: np.ndarray, cols: int, rows: int) -> Text:
    """Fit `pixels` (H, W, 3) into a `cols` x `rows` character grid, letterboxed and centered."""
    if cols <= 0 or rows <= 0:
        return Text("")

    src_h, src_w = pixels.shape[:2]
    if src_h <= 0 or src_w <= 0:
        return Text("")

    pixel_rows_avail = rows * 2
    scale = min(cols / src_w, pixel_rows_avail / src_h)
    fit_w = max(1, int(src_w * scale))
    fit_h = max(2, int(src_h * scale) & ~1)  # even, so it splits evenly into half-block rows

    resized = _resample_nearest(pixels, fit_w, fit_h)

    pad_x = (cols - fit_w) // 2
    fit_rows = fit_h // 2
    pad_y = (rows - fit_rows) // 2

    text = Text()
    for r in range(rows):
        if r > 0:
            text.append("\n")

        img_row = r - pad_y
        if img_row < 0 or img_row >= fit_rows:
            continue

        text.append(" " * pad_x)
        top = resized[img_row * 2]
        bottom = resized[img_row * 2 + 1]
        for x in range(fit_w):
            tr, tg, tb = top[x]
            br, bg, bb = bottom[x]
            text.append(_UPPER_HALF_BLOCK, style=f"rgb({tr},{tg},{tb}) on rgb({br},{bg},{bb})")
        text.append(" " * (cols - pad_x - fit_w))

    return text


def _resample_nearest(pixels: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    src_h, src_w = pixels.shape[:2]
    xs = (np.arange(out_w) * src_w // out_w).clip(0, src_w - 1)
    ys = (np.arange(out_h) * src_h // out_h).clip(0, src_h - 1)
    return pixels[ys][:, xs]
