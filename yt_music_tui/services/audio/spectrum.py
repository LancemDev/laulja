from __future__ import annotations

from typing import List

import numpy as np

"""
Turns raw 16-bit PCM audio into a small set of smoothed magnitude bars for the TUI's bar
visualizer. Uses numpy's FFT (the original hand-rolls a radix-2 FFT in C# with no external
dependency; numpy is the idiomatic Python equivalent). Mirrors SpectrumAnalyzer.cs.
"""

WINDOW_SIZE = 1024
BAR_COUNT = 24
DECAY = 0.75


class SpectrumAnalyzer:
    def __init__(self) -> None:
        self._window = np.zeros(WINDOW_SIZE, dtype=np.float32)
        self._fill = 0
        self._bars = np.zeros(BAR_COUNT, dtype=np.float64)
        self._hann = np.hanning(WINDOW_SIZE)
        self.bars: List[int] = [0] * BAR_COUNT

    def feed(self, pcm: bytes) -> None:
        """pcm: interleaved 16-bit signed little-endian stereo samples."""
        samples = np.frombuffer(pcm, dtype="<i2")
        frame_count = len(samples) // 2
        if frame_count == 0:
            return

        stereo = samples[: frame_count * 2].reshape(-1, 2).astype(np.float32)
        mono = (stereo[:, 0] + stereo[:, 1]) / 2.0 / 32767.0

        offset = 0
        remaining = frame_count
        while remaining > 0:
            space = WINDOW_SIZE - self._fill
            take = min(space, remaining)
            self._window[self._fill : self._fill + take] = mono[offset : offset + take]
            self._fill += take
            offset += take
            remaining -= take

            if self._fill == WINDOW_SIZE:
                self._analyze()
                self._fill = 0

    def _analyze(self) -> None:
        windowed = self._window * self._hann
        magnitude = np.abs(np.fft.rfft(windowed))

        usable_bins = len(magnitude)
        bins_per_bar = max(1, usable_bins // BAR_COUNT)

        next_bars = [0] * BAR_COUNT
        for bar in range(BAR_COUNT):
            start = bar * bins_per_bar
            end = min(usable_bins, start + bins_per_bar)
            peak = float(magnitude[start:end].max()) if end > start else 0.0

            # Decay smoothing so bars fall gracefully instead of snapping to zero between windows.
            self._bars[bar] = max(peak, self._bars[bar] * DECAY)
            next_bars[bar] = int(min(max(self._bars[bar] * 4000, 0), 100))

        self.bars = next_bars
