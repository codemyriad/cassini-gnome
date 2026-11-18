# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless draft transcription. Capture offers PCM; one consumer owns inference."""

import os
import threading
import wave
from dataclasses import dataclass
from queue import Empty, Full, Queue


@dataclass(frozen=True)
class CapturedPCM:
    source: str
    offset_ms: float
    frame: int
    rate: int
    channels: int
    data: bytes

    @property
    def frames(self):
        return len(self.data) // (2 * self.channels)


@dataclass(frozen=True)
class MissingPCM:
    source: str
    offset_ms: float
    rate: int
    channels: int
    first: int
    end: int


class LiveAudio:
    """One recording's bounded handoff. A full queue costs preview coverage."""

    def __init__(self, capacity=32):
        if capacity < 1:
            raise ValueError("Live audio requires a bounded positive capacity")
        self._queue = Queue(capacity)
        self.dropped_blocks = 0
        self.missing = {}

    def offer(self, block):
        try:
            self._queue.put_nowait(block)
        except Full:
            self.dropped_blocks += 1
            gaps = self.missing.setdefault(block.source, [])
            first = block.frame
            if gaps and gaps[-1].end == first:
                first = gaps.pop().first
            gaps.append(
                MissingPCM(
                    block.source,
                    block.offset_ms,
                    block.rate,
                    block.channels,
                    first,
                    block.frame + block.frames,
                )
            )

    def get(self, timeout=None):
        return self._queue.get(timeout=timeout)


@dataclass(frozen=True)
class LiveWord:
    start_ms: float
    end_ms: float
    text: str

    @property
    def midpoint_ms(self):
        return (self.start_ms + self.end_ms) / 2


@dataclass(frozen=True)
class DraftUpdate:
    source: str
    replace_from_ms: float
    through_ms: float
    settled_through_ms: float
    words: tuple[LiveWord, ...]


@dataclass(frozen=True)
class DraftResult:
    updates: tuple[DraftUpdate, ...]
    needs_saved_audio: bool


class DraftTranscript:
    """Consumer-owned snapshot. A late repair replaces only its own range."""

    def __init__(self):
        self._words = {}
        self._settled = {}

    def apply(self, update):
        words = self._words.get(update.source, ())
        self._words[update.source] = tuple(
            sorted(
                [word for word in words if not update.replace_from_ms <= word.midpoint_ms < update.through_ms]
                + list(update.words),
                key=lambda word: word.midpoint_ms,
            )
        )
        self._settled[update.source] = max(
            self._settled.get(update.source, 0),
            update.settled_through_ms,
        )

    def snapshot(self):
        return {source: (words, self._settled[source]) for source, words in self._words.items()}


class _Window:
    def __init__(self, block):
        self.start = self.end = block.frame
        self.settled = block.offset_ms + block.frame * 1000 / block.rate
        self.offset_ms = block.offset_ms
        self.rate = block.rate
        self.frame_bytes = 2 * block.channels
        self.pcm = bytearray()
        self.decoded = self.end


class LiveTranscription:
    """Feed one consumer in audio order. Updates replace the previous draft tail.

    Clock positions identify capture spans, which may represent a mixed source.
    A discontinuity flushes and resets that source's recognition window.
    """

    def __init__(self, recognizer, *, interval=1.5, context=2, window=8):
        if not 0 < interval <= window - context or not 0 < context < window <= 8:
            raise ValueError("Invalid live recognition window")
        self.recognizer = recognizer
        self.interval = interval
        self.context = context
        self.window = window
        self.sources = {}
        self.unsupported_sources = set()
        self.finished = False

    def accept(self, block):
        if self.finished:
            raise RuntimeError("Live transcription is finished")
        if block.rate not in (16000, 48000) or block.channels not in (1, 2):
            # Companion accepts a wider range than preview's pinned resampler.
            # Keep other sources live and leave this one to the saved-audio job.
            self.unsupported_sources.add(block.source)
            return []
        if block.frame < 0 or len(block.data) % (2 * block.channels) or block.frames == 0:
            raise ValueError("Invalid live audio frames")
        state = self.sources.get(block.source)
        updates = []
        if state is not None and (
            state.rate != block.rate or state.offset_ms != block.offset_ms or state.frame_bytes != 2 * block.channels
        ):
            raise ValueError("Live source geometry changed within a capture span")
        if state is None or state.end != block.frame:
            if state is not None:
                if block.frame < state.end:
                    raise ValueError("Live audio frames moved backwards")
                updates.extend(self._decode(block.source, state, final=True))
            state = self.sources[block.source] = _Window(block)
        stride = max(1, round(self.interval * block.rate))
        first = 0
        while first < block.frames:
            count = min(block.frames - first, stride - (state.end - state.decoded))
            state.pcm.extend(block.data[first * state.frame_bytes : (first + count) * state.frame_bytes])
            first += count
            state.end += count
            if state.end - state.decoded == stride:
                updates.extend(self._decode(block.source, state))
        return updates

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        return [update for source, state in self.sources.items() for update in self._decode(source, state, final=True)]

    def drain(self, audio, *, saved_tracks=None):
        """Call after capture stops, optionally supplying source-to-WAV paths for repair."""
        updates = []
        while True:
            try:
                block = audio.get(timeout=0)
            except Empty:
                break
            updates.extend(self.accept(block))
        updates.extend(self.finish())
        needs_saved_audio = bool(self.unsupported_sources)
        for source, gaps in audio.missing.items():
            path = saved_tracks.get(source) if saved_tracks is not None else None
            for gap in gaps:
                if path is None or gap.rate not in (16000, 48000) or gap.channels not in (1, 2):
                    needs_saved_audio = True
                    continue
                try:
                    recovered = self._recover(path, gap)
                except (OSError, EOFError, wave.Error):
                    needs_saved_audio = True
                else:
                    updates.extend(recovered)
        return DraftResult(tuple(updates), needs_saved_audio)

    def _recover(self, path, gap):
        repair = LiveTranscription(
            self.recognizer,
            interval=self.interval,
            context=self.context,
            window=self.window,
        )
        updates = []
        with wave.open(str(path), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (gap.rate, gap.channels, 2):
                raise OSError("Saved audio geometry differs from the capture")
            if gap.end > wav.getnframes():
                raise OSError("Saved audio ends before the missing range")
            wav.setpos(gap.first)
            for at in range(gap.first, gap.end, gap.rate):
                count = min(gap.rate, gap.end - at)
                data = wav.readframes(count)
                if len(data) != count * gap.channels * 2:
                    raise OSError("Saved audio ended during repair")
                updates.extend(repair.accept(CapturedPCM(gap.source, gap.offset_ms, at, gap.rate, gap.channels, data)))
        return updates + repair.finish()

    def _decode(self, source, state, *, final=False):
        if state.end == state.start:
            return []
        base = state.offset_ms + state.start * 1000 / state.rate
        end = state.offset_ms + state.end * 1000 / state.rate
        replace = state.settled
        words = []
        for word in self.recognizer.decode_window(state.pcm, state.rate, state.frame_bytes // 2):
            start_ms = min(end, max(base, base + word["startMs"]))
            end_ms = min(end, max(start_ms, base + word["endMs"]))
            if replace <= (start_ms + end_ms) / 2 < end:
                words.append(LiveWord(start_ms, end_ms, word["text"]))
        state.decoded = state.end
        # Retain room for the next decode and keep at least the requested context.
        advance = max(
            0, state.end - state.start - (round(self.window * state.rate) - round(self.interval * state.rate))
        )
        if final:
            state.settled = end
            state.pcm.clear()
            state.start = state.end
        else:
            boundary = max(replace, end - self.context * 1000)
            state.settled = boundary
            if state.rate == 48000:
                advance = min(state.end - state.start, ((advance + 2) // 3) * 3)
            if advance:
                del state.pcm[: advance * state.frame_bytes]
                state.start += advance
        return [DraftUpdate(source, replace, end, state.settled, tuple(words))]


class CapturePreview:
    """Optional, in-memory preview owned by one normal app capture."""

    _admission = threading.BoundedSemaphore(1)

    @classmethod
    def configured(cls):
        model = os.environ.get("CASSINI_LIVE_PREVIEW")
        return cls(model) if model in ("whistle", "parakeet") else None

    def __init__(self, model):
        self.model = model
        self.audio = LiveAudio()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._draft = DraftTranscript()
        self._started = False
        self._state = "waiting"

    def offer(self, block):
        if not self._started:
            self._started = True
            if not self._admission.acquire(blocking=False):
                self._status("busy")
                return
            try:
                worker = threading.Thread(target=self._run, name="cassini-live-preview", daemon=True)
                worker.start()
            except RuntimeError:
                self._admission.release()
                self._status("unavailable")
                return
            self._status("running", from_state="waiting")
        if self._state == "running":
            self.audio.offer(block)

    def close(self):
        self._stopped.set()
        self._status("finished", from_state="waiting")

    def snapshot(self):
        with self._lock:
            return self._state, self._draft.snapshot()

    def _status(self, value, *, from_state=None):
        with self._lock:
            if from_state is None or self._state == from_state:
                self._state = value

    def _apply(self, updates):
        with self._lock:
            for update in updates:
                self._draft.apply(update)

    def _client(self):
        from .transcription import bundle_path

        if self.model == "whistle":
            from .whistle_client import WhistleClient

            return WhistleClient(bundle_path(), timeout=10)
        from .parakeet_client import ParakeetClient
        from .parakeet_model import model_path
        from .sherpa_runtime import runtime_path

        return ParakeetClient(bundle_path(), model_path(), runtime_path(), timeout=15)

    def _run(self):
        try:
            with self._client() as client:
                live = LiveTranscription(client)
                while not self._stopped.is_set():
                    try:
                        block = self.audio.get(timeout=0.1)
                    except Empty:
                        continue
                    self._apply(live.accept(block))
                # The real saved-audio job owns publication. In particular it
                # can map rotated spans or repair any skipped preview.
                result = live.drain(self.audio)
                self._apply(result.updates)
                self._status("incomplete" if result.needs_saved_audio else "finished")
        except Exception:
            self._status("unavailable")
        finally:
            self._admission.release()
