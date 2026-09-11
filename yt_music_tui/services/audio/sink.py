from __future__ import annotations

import queue
import shutil
import subprocess
import threading
from typing import List, Optional, Union

"""
Launches an audio sink that accepts raw 16-bit PCM written to a Popen-shaped `.stdin`. Tries
PortAudio (via the pip-installable `sounddevice` package) first — works out of the box on
Linux, macOS and Windows with no system audio player needed — then falls back to shelling out
to PipeWire, PulseAudio, or ALSA in turn, for Linux systems where PortAudio itself isn't
available. Mirrors AudioSink.cs, extended with the PortAudio path.
"""

Sink = Union[subprocess.Popen, "_SoundDeviceSink"]

# ~512KB of s16le stereo audio at 44.1kHz — enough queued audio to smooth over scheduling
# jitter without letting memory grow unbounded if the decoder outruns real-time playback.
_CHUNK_QUEUE_DEPTH = 32


def start(sample_rate: int, channels: int) -> Optional[Sink]:
    return (
        _try_sounddevice(sample_rate, channels)
        or _try_start(
            "pw-play",
            [
                "--raw", "--format", "s16",
                "--rate", str(sample_rate), "--channels", str(channels), "-",
            ],
        )
        or _try_start(
            "paplay",
            ["--raw", f"--rate={sample_rate}", f"--channels={channels}", "--format=s16le"],
        )
        or _try_start(
            "aplay",
            ["-q", "-f", "S16_LE", "-r", str(sample_rate), "-c", str(channels)],
        )
    )


def _try_start(command: str, args: List[str]) -> Optional[subprocess.Popen]:
    path = shutil.which(command)
    if not path:
        return None
    try:
        return subprocess.Popen(
            [path, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


def _try_sounddevice(sample_rate: int, channels: int) -> Optional["_SoundDeviceSink"]:
    try:
        return _SoundDeviceSink(sample_rate, channels)
    except Exception:
        # No PortAudio host available (e.g. libportaudio2 not installed on this Linux system)
        # — the CLI-player fallbacks below still cover that case.
        return None


class _SoundDeviceSink:
    """Popen-lookalike wrapping a sounddevice.RawOutputStream, so player.py's
    sink.stdin.write()/flush()/close() and sink.kill()/wait() calls work unchanged regardless
    of which backend `start()` picked. Writes are handed off to a background thread over a
    bounded queue — sd.RawOutputStream.write() blocks until PortAudio can accept more data, and
    we don't want that blocking the asyncio event loop thread."""

    def __init__(self, sample_rate: int, channels: int) -> None:
        import sounddevice as sd  # imported lazily — only needed if this backend is picked

        self._stream = sd.RawOutputStream(samplerate=sample_rate, channels=channels, dtype="int16")
        self._stream.start()
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=_CHUNK_QUEUE_DEPTH)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self.stdin = self  # write()/flush()/close() below double as the "stdin" pipe

    # -- Popen.stdin-shaped surface, used by player.py's normal write path ------------------
    def write(self, chunk: bytes) -> None:
        try:
            self._queue.put(chunk, timeout=5.0)
        except queue.Full:
            pass  # device stalled — drop rather than block the pipeline indefinitely

    def flush(self) -> None:
        pass  # nothing buffered client-side beyond the queue itself

    def close(self) -> None:
        # Graceful end-of-track: let whatever's already queued finish playing before the
        # stream stops, same as closing stdin on a real player process.
        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            self._stopped.set()

    # -- Popen-shaped surface, used by player.py's teardown path -----------------------------
    def kill(self) -> None:
        # Abrupt stop (track skip/app exit): don't wait for the queue to drain.
        self._stopped.set()
        try:
            self._stream.abort()
        except Exception:
            pass

    def wait(self) -> None:
        self._thread.join(timeout=5.0)

    def _pump(self) -> None:
        try:
            while not self._stopped.is_set():
                chunk = self._queue.get()
                if chunk is None:
                    break
                try:
                    self._stream.write(chunk)
                except Exception:
                    break
        finally:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stopped.set()
