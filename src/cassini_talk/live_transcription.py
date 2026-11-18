# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless draft transcription. Capture offers PCM; one consumer owns inference."""

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
                [word for word in words if not update.replace_from_ms <= word.midpoint_ms <= update.through_ms]
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
        self.pcm = None
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
        self.finished = False

    def accept(self, block):
        if self.finished:
            raise RuntimeError("Live transcription is finished")
        if block.rate not in (16000, 48000) or block.channels not in (1, 2):
            raise ValueError("Invalid live PCM format")
        if block.frame < 0 or len(block.data) % (2 * block.channels) or block.frames == 0:
            raise ValueError("Invalid live audio frames")
        state = self.sources.get(block.source)
        updates = []
        if state is not None and (state.rate != block.rate or state.offset_ms != block.offset_ms):
            raise ValueError("Live source geometry changed within a capture span")
        if state is None or state.end != block.frame:
            if state is not None:
                if block.frame < state.end:
                    raise ValueError("Live audio frames moved backwards")
                updates.extend(self._decode(block.source, state, final=True))
            state = self.sources[block.source] = _Window(block)
        import numpy as np

        pcm = np.frombuffer(block.data, dtype="<i2").reshape(-1, block.channels)
        pcm = pcm.mean(axis=1, dtype=np.float32) / np.float32(32768)
        stride = max(1, round(self.interval * block.rate))
        first = 0
        while first < len(pcm):
            count = min(len(pcm) - first, stride - (state.end - state.decoded))
            part = pcm[first : first + count]
            state.pcm = part if state.pcm is None else np.concatenate((state.pcm, part))
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
        needs_saved_audio = False
        for source, gaps in audio.missing.items():
            path = saved_tracks.get(source) if saved_tracks is not None else None
            for gap in gaps:
                if path is None:
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
        import numpy as np

        from .speech_pipeline import fir_values

        if state.end == state.start:
            return []
        pcm = state.pcm
        if state.rate == 48000:
            pcm = fir_values(pcm, 0, 0, (len(pcm) + 2) // 3)
        base = state.offset_ms + state.start * 1000 / state.rate
        end = state.offset_ms + state.end * 1000 / state.rate
        replace = state.settled
        words = []
        if np.any(pcm):
            for word in self.recognizer.decode_window(pcm):
                start_ms = min(end, max(base, base + word["startMs"]))
                end_ms = min(end, max(start_ms, base + word["endMs"]))
                if (start_ms + end_ms) / 2 >= replace:
                    words.append(LiveWord(start_ms, end_ms, word["text"]))
        state.decoded = state.end
        # Retain room for the next decode and keep at least the requested context.
        advance = max(0, len(state.pcm) - (round(self.window * state.rate) - round(self.interval * state.rate)))
        if final:
            state.settled = end
            state.pcm = np.empty(0, dtype=np.float32)
            state.start = state.end
        else:
            boundary = max(replace, end - self.context * 1000)
            state.settled = boundary
            if state.rate == 48000:
                advance = min(len(state.pcm), ((advance + 2) // 3) * 3)
            if advance:
                state.pcm = state.pcm[advance:].copy()
                state.start += advance
        return [DraftUpdate(source, replace, end, state.settled, tuple(words))]
