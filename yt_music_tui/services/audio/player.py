from __future__ import annotations

import asyncio
import collections
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
        # True when self._queue was seeded by play_track_radio() rather than play_queue() — a
        # library/search selection or a playlist, respectively. Only a radio queue auto-extends
        # itself with more similar-vibe tracks once it's played through; a playlist just loops.
        self._queue_is_radio = False

        self._ffmpeg: Optional[asyncio.subprocess.Process] = None
        self._sink = None  # subprocess.Popen — stdin writes happen on a worker thread
        self._pump_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._ffmpeg_stderr_tail: str = ""
        self._samples_written = 0
        self._is_playing = False
        self._track_ended = False
        # Callers (the UI's key handler, and tick()'s own auto-advance) no longer await a
        # play/skip through to completion before doing anything else, so a rapid pair of skips
        # could otherwise start two _start_current() calls concurrently and corrupt playback
        # state (each racing to set self.current/self._index/self._ffmpeg). This serializes them
        # instead: a second call just waits for the first to finish before it starts its own.
        self._action_lock = asyncio.Lock()

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
        async with self._action_lock:
            self._queue = [track]
            self._index = 0
            self._queue_is_radio = False
            await self._start_current()

    async def play_queue(self, tracks: List[Track], start_index: int = 0) -> None:
        async with self._action_lock:
            self._queue = list(tracks)
            self._index = max(0, min(start_index, len(self._queue) - 1)) if self._queue else -1
            self._queue_is_radio = False

            if not self._queue:
                await self._stop_pipeline()
                self.current = None
                self.duration_seconds = 0.0
                return

            await self._start_current()

    async def play_track_radio(self, track: Track) -> None:
        """Plays `track` the way YouTube Music itself does when you pick an individual song
        outside a playlist: starts a radio/mix seeded from it and queues that, so playback keeps
        going with similar-vibe tracks instead of just looping the one song. Falls back to
        looping `track` alone if the radio fetch fails or comes back empty."""
        async with self._action_lock:
            try:
                tracks = await self._music.get_radio_tracks(track.id)
            except Exception:
                tracks = []
            if not tracks:
                tracks = [track]

            self._queue = tracks
            self._queue_is_radio = True
            # The radio isn't guaranteed to list the seed track first (or at all) — make sure
            # playback actually starts on the track the user picked, not track 0 of the mix.
            self._index = next((i for i, t in enumerate(tracks) if t.id == track.id), 0)
            await self._start_current()

    async def toggle_pause(self) -> None:
        if self.current is None:
            return
        self._is_playing = not self._is_playing

    async def next_track(self) -> None:
        async with self._action_lock:
            if not self._queue:
                return
            if self._queue_is_radio and self._index == len(self._queue) - 1:
                await self._extend_radio()
            self._index = (self._index + 1) % len(self._queue)
            await self._start_current()

    async def _extend_radio(self) -> None:
        """The radio queue's last track is about to finish — fetch more tracks in that same
        vibe, seeded from it, so playback keeps going instead of wrapping back to the first
        track. Mirrors YouTube Music's own endless-mix behavior. Leaves the queue untouched on
        failure; next_track()'s modulo wraparound is still a reasonable fallback."""
        seed = self._queue[self._index]
        try:
            more = await self._music.get_radio_tracks(seed.id)
        except Exception:
            return
        existing_ids = {t.id for t in self._queue}
        self._queue.extend(t for t in more if t.id not in existing_ids)

    async def previous_track(self) -> None:
        async with self._action_lock:
            if not self._queue:
                return
            self._index = (self._index - 1) % len(self._queue)
            await self._start_current()

    async def play_at(self, index: int) -> None:
        """Jumps straight to a specific queue position — the queue view's "play this one now"
        action, rather than stepping one track at a time via next/previous."""
        async with self._action_lock:
            if not (0 <= index < len(self._queue)):
                return
            self._index = index
            await self._start_current()

    async def remove_at(self, index: int) -> Optional[str]:
        """Removes the track at `index` from the queue — the queue view's quick-remove action.
        Refuses to remove the track currently playing (skip/previous is what that's for) rather
        than yank it out from under the in-flight ffmpeg pipeline. Returns an error message on
        failure, None on success."""
        async with self._action_lock:
            if not (0 <= index < len(self._queue)):
                return "Invalid queue position"
            if index == self._index:
                return "Can't remove the track that's currently playing"

            del self._queue[index]
            if index < self._index:
                self._index -= 1
            return None

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
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                # YouTube's CDN (googlevideo) URLs occasionally drop the connection mid-stream
                # before the whole track has been sent. Without reconnect handling, ffmpeg just
                # treats that dropped connection as EOF and exits — which _pump can't tell apart
                # from a genuinely finished track, so playback silently stops short instead of
                # erroring or retrying.
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
                "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
                "-i", url,
                "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
        self._ffmpeg_stderr_tail = ""
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(ffmpeg))
        self._pump_task = asyncio.ensure_future(self._pump(ffmpeg, sink))

    async def _drain_stderr(self, ffmpeg: asyncio.subprocess.Process) -> None:
        """Continuously reads ffmpeg's stderr into a bounded tail buffer for `_pump` to surface
        if the process exits unexpectedly. Has to run for the process's whole lifetime rather
        than reading it after exit — piped stderr has a small OS buffer, and if nothing drains
        it while ffmpeg is still running, ffmpeg can block trying to write to it (e.g. while
        retrying a dropped connection) and never get to actually producing more audio."""
        assert ffmpeg.stderr is not None
        tail: collections.deque = collections.deque(maxlen=40)
        try:
            while True:
                line = await ffmpeg.stderr.readline()
                if not line:
                    break
                tail.append(line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass  # expected on stop/skip — the pipeline is being torn down deliberately
        except Exception:
            pass
        finally:
            self._ffmpeg_stderr_tail = "\n".join(tail)

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
                    await self._check_incomplete_stream(ffmpeg)
                    self._track_ended = True
                    return

                await loop.run_in_executor(None, self._write_to_sink, sink, chunk)
                self._analyzer.feed(chunk)
                self._samples_written += len(chunk) // BYTES_PER_FRAME
        except asyncio.CancelledError:
            pass  # expected on stop/skip — the pipeline is being torn down deliberately
        except Exception:
            self._track_ended = True

    async def _check_incomplete_stream(self, ffmpeg: asyncio.subprocess.Process) -> None:
        """ffmpeg's stdout closing normally means it decoded the whole track — but it also
        closes on a fatal input error (e.g. the CDN dropping the connection and reconnect
        exhausting its retries), which looks identical to `_pump` beyond this point. Distinguish
        the two by exit code and how much of the track's known duration was actually decoded, so
        a stream that failed partway surfaces as an error instead of silently passing for a
        track that simply ended."""
        try:
            returncode = await asyncio.wait_for(ffmpeg.wait(), timeout=2.0)
        except Exception:
            return
        if returncode == 0:
            return

        # The process exiting doesn't guarantee _drain_stderr has already gotten scheduled to
        # read its final EOF and flush the tail buffer — give it a brief chance to catch up
        # before reading self._ffmpeg_stderr_tail below, without risking a real hang if it
        # doesn't (shield so this timeout doesn't cancel the task; _stop_pipeline owns that).
        if self._stderr_task and not self._stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=1.0)
            except Exception:
                pass

        played = self.position_seconds
        if self.duration_seconds and played >= self.duration_seconds * 0.95:
            return  # near enough the real end — not worth flagging over a rounding gap

        detail = self._ffmpeg_stderr_tail.strip().splitlines()
        where = f"{played:.0f}s of {self.duration_seconds:.0f}s" if self.duration_seconds else f"{played:.0f}s"
        self.last_error = (
            f"Stream cut short at {where} (ffmpeg exit {returncode}"
            + (f": {detail[-1]}" if detail else "")
            + ")"
        )

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

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

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
