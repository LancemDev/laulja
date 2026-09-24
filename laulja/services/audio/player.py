from __future__ import annotations

import asyncio
import collections
import shutil
import time
from typing import List, Optional, Tuple

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
_BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_FRAME

# Decoded audio is read ahead of playback into an in-process buffer, so a network stall shorter
# than the buffer is inaudible: the reader keeps ffmpeg's output pipe drained (up to
# _READAHEAD_BYTES ahead), and the feeder only hands audio to the sink from that buffer.
_READ_CHUNK = 16384
_READAHEAD_BYTES = 30 * _BYTES_PER_SECOND
# Audio to accumulate before a track first starts, and before resuming after the buffer runs dry —
# resuming the instant one chunk arrives would just stall again a moment later.
_PREBUFFER_BYTES = 2 * _BYTES_PER_SECOND
_REBUFFER_BYTES = 5 * _BYTES_PER_SECOND

# Adaptive quality: this many buffer underruns within one track flips upcoming tracks to the
# lower-bitrate stream, and this many consecutive stall-free tracks flips back. A first-buffer
# that takes longer than _SLOW_START_SECONDS to fill counts as an underrun too.
_UNDERRUNS_TO_DOWNGRADE = 2
_SMOOTH_TRACKS_TO_UPGRADE = 5
_SLOW_START_SECONDS = 8.0

# Stream URLs are short-lived (hours), so a prefetched one is only trusted for a while.
_PREFETCH_TTL_SECONDS = 20 * 60


class AudioPlayerService:
    def __init__(self, music: MusicService, force_low_bandwidth: bool = False):
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

        # Read-ahead buffer between ffmpeg (reader task) and the audio sink (feeder = _pump).
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._buffered_bytes = 0
        self._reader_done = False
        self._buffering = False

        # Adaptive quality (see _note_underrun / _note_track_finished). `_force_low_bandwidth` is
        # the user's explicit setting; `_auto_low_bandwidth` is what stalls have told us.
        self._force_low_bandwidth = force_low_bandwidth
        self._auto_low_bandwidth = False
        self._track_underruns = 0
        self._smooth_tracks = 0

        # Stream URL for the next queue entry, resolved while the current one plays.
        self._prefetch_task: Optional[asyncio.Task] = None
        self._prefetch_key: Optional[Tuple[str, bool]] = None
        # Callers (the UI's key handler, and tick()'s own auto-advance) no longer await a
        # play/skip through to completion before doing anything else, so a rapid pair of skips
        # could otherwise start two _start_current() calls concurrently and corrupt playback
        # state (each racing to set self.current/self._index/self._ffmpeg). This serializes them
        # instead: a second call just waits for the first to finish before it starts its own.
        self._action_lock = asyncio.Lock()

        self.current: Optional[Track] = None
        self.duration_seconds: float = 0.0
        self.last_error: Optional[str] = None
        # One-shot informational message for the UI (e.g. "switched to lower quality"), cleared
        # by whoever displays it — unlike last_error, it isn't a playback failure.
        self.notice: Optional[str] = None

    @property
    def is_buffering(self) -> bool:
        """True while playback is waiting for more audio to arrive (start-up or after a stall)."""
        return self._buffering and self._is_playing

    @property
    def is_low_bandwidth(self) -> bool:
        return self._force_low_bandwidth or self._auto_low_bandwidth

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def position_seconds(self) -> float:
        # Prefer frames the sink has actually handed to the audio device over frames merely
        # written to it — the sounddevice backend queues several chunks ahead of playback
        # (see sink.py's `frames_played`), so counting on write alone runs seconds ahead of
        # what's actually audible, throwing off anything timed against position (synced lyrics).
        # Only _SoundDeviceSink exposes this; the CLI-player fallbacks (pw-play/paplay/aplay)
        # have no such visibility, so fall back to samples handed to their stdin pipe.
        frames_played = getattr(self._sink, "frames_played", None)
        if frames_played is not None:
            return frames_played / SAMPLE_RATE
        return self._samples_written / SAMPLE_RATE

    @property
    def queue(self) -> List[Track]:
        return self._queue

    @property
    def index(self) -> int:
        return self._index

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
        if not self._is_playing:
            # Only _SoundDeviceSink queues audio this deeply ahead of the device (see
            # sink.py's clear_pending) — the CLI-player fallbacks have no equivalent backlog to
            # drop, so this is a no-op for them via getattr rather than an isinstance check.
            clear_pending = getattr(self._sink, "clear_pending", None)
            if clear_pending is not None:
                clear_pending()

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
            if not self._queue or self._index <= 0:
                return
            self._index -= 1
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
            url = await self._resolve_url(self.current)
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
        self._buffer = asyncio.Queue()
        self._buffered_bytes = 0
        self._reader_done = False
        self._buffering = True
        self._track_underruns = 0
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(ffmpeg))
        self._reader_task = asyncio.ensure_future(self._read_ahead(ffmpeg))
        self._pump_task = asyncio.ensure_future(self._pump(ffmpeg, sink))
        self._schedule_prefetch()

    async def _resolve_url(self, track: Track) -> str:
        low = self.is_low_bandwidth
        if self._prefetch_key == (track.id, low) and self._prefetch_task is not None:
            task, self._prefetch_task, self._prefetch_key = self._prefetch_task, None, None
            result = await task
            if result is not None and time.monotonic() - result[1] < _PREFETCH_TTL_SECONDS:
                return result[0]
        return await self._music.get_stream_url(track.id, low)

    def _schedule_prefetch(self) -> None:
        """Resolves the next queue entry's stream URL in the background while the current track
        plays, so skipping or auto-advancing doesn't wait on a fresh resolve (a couple of seconds
        on a good network, far more on a slow one). A radio queue's last entry is skipped —
        advancing there extends the queue first, so there's no fixed "next" yet."""
        if not self._queue or self._index < 0:
            return
        next_index = self._index + 1
        if next_index >= len(self._queue):
            if self._queue_is_radio:
                return
            next_index = 0
        track = self._queue[next_index]
        if track.id == self._queue[self._index].id:
            return

        key = (track.id, self.is_low_bandwidth)
        if self._prefetch_key == key:
            return
        self._cancel_prefetch()
        self._prefetch_key = key

        async def prefetch() -> Optional[Tuple[str, float]]:
            try:
                return await self._music.get_stream_url(track.id, key[1]), time.monotonic()
            except Exception:
                return None  # not fatal — playback just resolves it normally when it's needed

        self._prefetch_task = asyncio.ensure_future(prefetch())

    def _cancel_prefetch(self) -> None:
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetch_key = None

    async def _read_ahead(self, ffmpeg: asyncio.subprocess.Process) -> None:
        """Keeps draining ffmpeg's decoded output into the read-ahead buffer, up to
        _READAHEAD_BYTES ahead of what's been played, so a slow or stalling network only becomes
        audible once the whole buffer has been used up."""
        assert ffmpeg.stdout is not None
        try:
            while True:
                while self._buffered_bytes >= _READAHEAD_BYTES:
                    await asyncio.sleep(0.05)
                chunk = await ffmpeg.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                self._buffered_bytes += len(chunk)
                self._buffer.put_nowait(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self._buffer.put_nowait(None)
        self._reader_done = True

    async def _fill_buffer(self, target_bytes: int) -> None:
        """Waits until `target_bytes` of audio are buffered (or the stream has ended)."""
        while self._buffered_bytes < target_bytes and not self._reader_done:
            await asyncio.sleep(0.05)

    def _note_underrun(self) -> None:
        self._track_underruns += 1
        self._smooth_tracks = 0
        if (
            self._track_underruns >= _UNDERRUNS_TO_DOWNGRADE
            and not self._auto_low_bandwidth
            and not self._force_low_bandwidth
        ):
            self._auto_low_bandwidth = True
            self.notice = "Slow network — using lower audio quality for upcoming tracks"
            self._schedule_prefetch()  # the already-prefetched URL was for the higher tier

    def _note_track_finished(self) -> None:
        if self._track_underruns:
            return
        self._smooth_tracks += 1
        if self._auto_low_bandwidth and self._smooth_tracks >= _SMOOTH_TRACKS_TO_UPGRADE:
            self._auto_low_bandwidth = False
            self._smooth_tracks = 0
            self.notice = "Network looks fine again — restoring full audio quality"
            self._schedule_prefetch()

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
            started = time.monotonic()
            await self._fill_buffer(_PREBUFFER_BYTES)
            if time.monotonic() - started > _SLOW_START_SECONDS:
                self._note_underrun()
            self._buffering = False

            while True:
                while not self._is_playing:
                    await asyncio.sleep(0.05)

                if self._buffer.empty() and not self._reader_done:
                    # Ran out of audio mid-track (the network can't keep up): hold off feeding
                    # the sink until a few seconds have built back up, rather than stuttering
                    # along a chunk at a time. The sink's own queue keeps playing meanwhile.
                    self._buffering = True
                    self._note_underrun()
                    await self._fill_buffer(_REBUFFER_BYTES)
                    self._buffering = False

                chunk = await self._buffer.get()
                if chunk is None:
                    # ffmpeg has decoded the whole track, but the sink (pw-play/paplay/aplay)
                    # can still be holding several hundred ms of already-written audio in its
                    # own internal buffer that hasn't reached the speakers yet. Closing its
                    # stdin and waiting for it to exit lets it actually finish playing that
                    # buffer; without this, _track_ended flips immediately, next_track() tears
                    # the pipeline down, and _stop_pipeline()'s sink.kill() silences that tail
                    # before it's heard — the track visibly (audibly) cuts short of the end.
                    await self._drain_sink(sink)
                    await self._check_incomplete_stream(ffmpeg)
                    if not self.last_error:
                        self._note_track_finished()
                    self._track_ended = True
                    return

                self._buffered_bytes -= len(chunk)
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

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self._buffering = False

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
        self._cancel_prefetch()
        await self._stop_pipeline()
