from __future__ import annotations

import asyncio
import shutil
from typing import List, Optional

from ...models import Track
from ..music_service import MusicService
from . import sink as audio_sink
from .spectrum import SpectrumAnalyzer

"""
Real playback: ffmpeg decodes the resolved stream URL to raw PCM, which is forwarded to a
system audio sink (see sink.py) and simultaneously fed into a SpectrumAnalyzer so the bar
visualizer reflects the actual audio, not a simulated animation. Position is derived from
samples actually consumed, not wall-clock time, so it stays accurate across pauses.
Mirrors AudioPlayerService.cs.
"""

SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2  # 16-bit samples


class AudioPlayerService:
    def __init__(self, music: MusicService):
        self._music = music
        self._analyzer = SpectrumAnalyzer()
        self._queue: List[Track] = []
        self._index = -1

        self._ffmpeg: Optional[asyncio.subprocess.Process] = None
        self._sink = None  # subprocess.Popen — stdin writes happen on a worker thread
        self._pump_task: Optional[asyncio.Task] = None
        self._samples_written = 0
        self._is_playing = False
        self._track_ended = False

        self.current: Optional[Track] = None
        self.duration_seconds: float = 0.0
        self.last_error: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def position_seconds(self) -> float:
        return self._samples_written / SAMPLE_RATE

    @property
    def queue(self) -> List[Track]:
        return self._queue

    @property
    def visualizer_levels(self) -> List[int]:
        return self._analyzer.bars

    async def play(self, track: Track) -> None:
        self._queue = [track]
        self._index = 0
        await self._start_current()

    async def play_queue(self, tracks: List[Track], start_index: int = 0) -> None:
        self._queue = list(tracks)
        self._index = max(0, min(start_index, len(self._queue) - 1)) if self._queue else -1

        if not self._queue:
            await self._stop_pipeline()
            self.current = None
            self.duration_seconds = 0.0
            return

        await self._start_current()

    async def toggle_pause(self) -> None:
        if self.current is None:
            return
        self._is_playing = not self._is_playing

    async def next_track(self) -> None:
        if not self._queue:
            return
        self._index = (self._index + 1) % len(self._queue)
        await self._start_current()

    async def previous_track(self) -> None:
        if not self._queue:
            return
        self._index = (self._index - 1) % len(self._queue)
        await self._start_current()

    def tick(self) -> None:
        if not self._track_ended:
            return
        self._track_ended = False
        asyncio.ensure_future(self.next_track())

    async def _start_current(self) -> None:
        await self._stop_pipeline()

        self.current = self._queue[self._index]
        self.duration_seconds = self.current.duration.total_seconds() if self.current.duration else 0.0
        self._samples_written = 0
        self.last_error = None

        try:
            url = await self._music.get_stream_url(self.current.id)
        except Exception as ex:
            self.last_error = f"Couldn't resolve stream for {self.current.title}: {ex}"
            self._is_playing = False
            return

        if not shutil.which("ffmpeg"):
            self.last_error = "ffmpeg not found — install it to enable playback."
            self._is_playing = False
            return

        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url,
                "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as ex:
            self.last_error = f"Couldn't start ffmpeg: {ex}"
            self._is_playing = False
            return

        sink = audio_sink.start(SAMPLE_RATE, CHANNELS)
        if sink is None:
            self.last_error = "No audio output found (tried pw-play, paplay, aplay)."
            ffmpeg.kill()
            self._is_playing = False
            return

        self._ffmpeg = ffmpeg
        self._sink = sink
        self._is_playing = True
        self._pump_task = asyncio.ensure_future(self._pump(ffmpeg, sink))

    async def _pump(self, ffmpeg: asyncio.subprocess.Process, sink) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                while not self._is_playing:
                    await asyncio.sleep(0.05)

                assert ffmpeg.stdout is not None
                chunk = await ffmpeg.stdout.read(16384)
                if not chunk:
                    # ffmpeg has decoded the whole track, but the sink (pw-play/paplay/aplay)
                    # can still be holding several hundred ms of already-written audio in its
                    # own internal buffer that hasn't reached the speakers yet. Closing its
                    # stdin and waiting for it to exit lets it actually finish playing that
                    # buffer; without this, _track_ended flips immediately, next_track() tears
                    # the pipeline down, and _stop_pipeline()'s sink.kill() silences that tail
                    # before it's heard — the track visibly (audibly) cuts short of the end.
                    await self._drain_sink(sink)
                    self._track_ended = True
                    return

                await loop.run_in_executor(None, self._write_to_sink, sink, chunk)
                self._analyzer.feed(chunk)
                self._samples_written += len(chunk) // BYTES_PER_FRAME
        except asyncio.CancelledError:
            pass  # expected on stop/skip — the pipeline is being torn down deliberately
        except Exception:
            self._track_ended = True

    @staticmethod
    async def _drain_sink(sink) -> None:
        try:
            sink.stdin.close()
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, sink.wait), timeout=5.0)
        except Exception:
            pass  # timed out or already gone — proceed rather than hang track advancement

    @staticmethod
    def _write_to_sink(sink, chunk: bytes) -> None:
        try:
            sink.stdin.write(chunk)
            sink.stdin.flush()
        except Exception:
            pass

    async def _stop_pipeline(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
            self._pump_task = None

        if self._ffmpeg:
            try:
                self._ffmpeg.kill()
            except ProcessLookupError:
                pass
            self._ffmpeg = None

        if self._sink:
            try:
                self._sink.stdin.close()
            except Exception:
                pass
            try:
                self._sink.kill()
            except Exception:
                pass
            self._sink = None

    async def dispose(self) -> None:
        await self._stop_pipeline()
