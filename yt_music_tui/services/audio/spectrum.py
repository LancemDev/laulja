from __future__ import annotations

from typing import List, Tuple

import numpy as np

"""
Turns raw 16-bit PCM audio into a small set of smoothed magnitude bars for the TUI's bar
visualizer. Uses numpy's FFT (the original hand-rolls a radix-2 FFT in C# with no external
dependency; numpy is the idiomatic Python equivalent). Mirrors SpectrumAnalyzer.cs.

Two things a naive port gets wrong for real music:
  - Dividing FFT bins evenly by *frequency* crams almost all musical energy (bass/mid) into
    the first handful of bars, which then just sit pegged at the ceiling — only the near-empty
    high-frequency tail has room to move. Bars are grouped on a log-frequency scale instead,
    like a real spectrum analyzer, so the whole row represents bass through treble.
  - A fixed linear scale factor either clips everything to max or leaves it near zero depending
    on how loud the track is. Bars are normalized against a slowly-decaying running peak instead
    (simple auto-gain), so quiet and loud passages both use the full 0-100 range.
"""

WINDOW_SIZE = 1024
BAR_COUNT = 24
DECAY = 0.75
SAMPLE_RATE = 44100  # must match AudioPlayerService.SAMPLE_RATE — feed() expects PCM at this rate
MIN_FREQ_HZ = 40.0
MAX_FREQ_HZ = 16000.0  # above this is mostly sizzle/noise for compressed streams — not worth a bar
REF_DECAY = 0.995  # how slowly the auto-gain reference peak relaxes back down between loud bits
MIN_REF_PEAK = 1e-3  # floor so a quiet passage after silence doesn't spike every bar to 100


class SpectrumAnalyzer:
    def __init__(self) -> None:
        self._window = np.zeros(WINDOW_SIZE, dtype=np.float32)
        self._fill = 0
        self._bars = np.zeros(BAR_COUNT, dtype=np.float64)
        self._hann = np.hanning(WINDOW_SIZE)
        self._ref_peak = MIN_REF_PEAK
        self._bands = _log_frequency_bands()
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

        peaks = [
            float(magnitude[start:end].max()) if end > start else 0.0
            for start, end in self._bands
        ]

        # Auto-gain: normalize against a running peak so quiet and loud passages both use the
        # full 0-100 range, instead of a fixed multiplier that clips loud bars and flatlines quiet ones.
        self._ref_peak = max(max(peaks), self._ref_peak * REF_DECAY, MIN_REF_PEAK)

        next_bars = [0] * BAR_COUNT
        for bar in range(BAR_COUNT):
            level = peaks[bar] / self._ref_peak * 100.0

            # Decay smoothing so bars fall gracefully instead of snapping to zero between windows.
            self._bars[bar] = max(level, self._bars[bar] * DECAY)
            next_bars[bar] = int(min(max(self._bars[bar], 0), 100))

        self.bars = next_bars


def _log_frequency_bands() -> List[Tuple[int, int]]:
    """Bin ranges for each bar, spaced logarithmically across MIN_FREQ_HZ..MAX_FREQ_HZ — like a
    real spectrum analyzer's octave bands, so bass, mids, and treble each get a fair share of bars
    instead of bass alone dominating the first few (linear spacing would give it most of the bins)."""
    freq_per_bin = SAMPLE_RATE / WINDOW_SIZE
    usable_bins = WINDOW_SIZE // 2 + 1
    max_bin = usable_bins - 1

    log_edges = np.logspace(np.log10(MIN_FREQ_HZ), np.log10(MAX_FREQ_HZ), BAR_COUNT + 1)
    bin_edges = np.clip((log_edges / freq_per_bin).astype(int), 0, max_bin)

    bands = []
    for i in range(BAR_COUNT):
        start = int(bin_edges[i])
        end = min(max(start + 1, int(bin_edges[i + 1])), usable_bins)
        bands.append((start, end))
    return bands
