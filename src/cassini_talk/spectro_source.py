# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 bounded, observer-owned analysis state for committed capture PCM."""

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction

from .spectro_analysis import Quantizer, Track, prepare_acceleration

LOG = logging.getLogger(__name__)


@dataclass(eq=False)
class Subscription:
    lanes: dict = field(default_factory=lambda: {side: deque(maxlen=64) for side in ("me", "others")})


@dataclass
class SourceTrack:
    analysis: Track | None
    side: str
    level: float
    arrival_ms: float
    chunk_ms: float = 20
    progress: int = -1
    automatic_level: bool = False


class SpectroSource:
    """Separate from the WAV lock, with one bounded output queue per observer.

    PCM belongs to its actual stored span ID and start offset. Only committed
    selected bytes may enter this API. Existing AC meters supply levels; the
    picture never supplies a permission, silence or recording decision.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.subscribers, self.tracks, self.retired = set(), {}, set()
        self.known = {}
        self.now_ms = 0
        self.problem = None
        self.finished = False
        self.selected, self.next_selection_ms = set(), 0
        self.pending = {side: {} for side in ("me", "others")}
        self.quantizers = {side: Quantizer() for side in ("me", "others")}
        self.released = {side: -1 for side in ("me", "others")}
        self.calls = self.analysed = self.analysis_ns = self.analysis_cpu_ns = self.late = self.overflow = 0

    @property
    def active(self):
        return bool(self.subscribers) and self.problem is None and not self.finished

    def observe(self, *args, **kwargs):
        try:
            self.feed(*args, **kwargs)
        except Exception as error:
            # A decorative picture must never turn accepted PCM into an audio
            # error, or prevent the next buffer from reaching the WAV writer.
            if self.problem is None:
                self.problem = str(error)
                LOG.exception("Recording sound indicator failed; saved audio is retained")

    def declare(self, identifier, *, kind, rate=48000, now_ms=0):
        with self.lock:
            if identifier in self.retired or len(self.known) >= 64 and identifier not in self.known:
                return
            side = "me" if kind in ("microphone", "local") else "others"
            chunk_ms = 8192 * 1000 / rate if kind in ("remote", "application", "tab", "media", "local") else 20
            self.known[identifier] = (side, chunk_ms)
            if self.active and identifier not in self.tracks:
                self.tracks[identifier] = SourceTrack(None, side, 0, now_ms, chunk_ms=chunk_ms)
                if sum(self.tracks[key].side == side for key in self.selected) < 3:
                    self.selected.add(identifier)

    def subscribe(self, now_ms=0):
        prepare_acceleration()
        with self.lock:
            if not self.subscribers:
                self.tracks = {
                    key: SourceTrack(None, side, 0, now_ms, chunk_ms=chunk) for key, (side, chunk) in self.known.items()
                }
                self.next_selection_ms = now_ms
            subscription = Subscription()
            self.subscribers.add(subscription)
            return subscription

    def unsubscribe(self, subscription):
        with self.lock:
            self.subscribers.discard(subscription)
            for queue in subscription.lanes.values():
                queue.clear()
            if not self.subscribers:
                # A return starts with empty history, rather than analysing
                # invisible audio or replaying it into a newly mapped strip.
                self.tracks.clear()
                self.selected.clear()
                self.pending = {side: {} for side in self.pending}
                self.quantizers = {side: Quantizer() for side in self.pending}
                self.released = {side: -1 for side in self.pending}
                self.next_selection_ms = 0

    def retire(self, identifier):
        with self.lock:
            self.retired.add(identifier)
            self.known.pop(identifier, None)
            self.tracks.pop(identifier, None)
            self.selected.discard(identifier)

    def _choose(self, now_ms, current_id, current_data):
        if now_ms < self.next_selection_ms:
            return
        self.next_selection_ms = now_ms + 1000
        from .recording_details import pcm_ac_level

        for key, track in self.tracks.items():
            analysis = track.analysis
            if track.automatic_level and analysis is not None:
                width = analysis.rate // 10 * analysis.channels * 2
                if key == current_id:
                    tail = current_data[-width:]
                    if len(tail) < width:
                        tail = bytes(analysis.raw_history[-(width - len(tail)) :]) + tail
                else:
                    tail = analysis.raw_history[-width:]
                track.level = pcm_ac_level(tail, analysis.channels) if tail else 0
        self.selected = set()
        for side in self.pending:
            candidates = [(key, value) for key, value in self.tracks.items() if value.side == side]
            # Real digitally silent PCM beats a never-arriving placeholder, so
            # silence has a trough rather than being shown as missing audio.
            candidates.sort(
                key=lambda pair: (
                    -pair[1].level,
                    -(pair[0] == current_id or bool(pair[1].analysis and pair[1].analysis.total)),
                    pair[0],
                )
            )
            self.selected.update(key for key, _track in candidates[:3])

    def feed(self, identifier, data, offset_ms, *, rate, channels, kind, start_ms, now_ms, level, gap_frames=0):
        with self.lock:
            if not self.active or identifier in self.retired:
                return
            started, cpu_started = time.perf_counter_ns(), time.thread_time_ns()
            self.calls += 1
            self.now_ms = now_ms
            track = self.tracks.get(identifier)
            if track is None:
                self.declare(identifier, kind=kind, rate=rate, now_ms=now_ms)
                track = self.tracks.get(identifier)
                if track is None:
                    return
            track.automatic_level = level is None
            if level is not None:
                track.level = level
            track.arrival_ms = now_ms
            if track.analysis is None:
                track.analysis = Track(rate, channels, start_ms=start_ms)
            analysis = track.analysis
            self._choose(now_ms, identifier, data)
            expected_ms = start_ms + analysis.total * 1000 / rate
            missing = max(0, round((offset_ms - expected_ms) * rate / 1000))
            gap = max(gap_frames, missing)
            track.chunk_ms = len(data) * 1000 / (channels * 2 * rate)
            selected = identifier in self.selected
            rows = analysis.feed(data, selected=selected, gap_frames=gap)
            if not selected:
                self.analysis_ns += time.perf_counter_ns() - started
                self.analysis_cpu_ns += time.thread_time_ns() - cpu_started
                return
            complete = (analysis.total // analysis.hop) * analysis.hop
            if analysis.integral_hop:
                track.progress = analysis.integer_origin + complete // analysis.hop
            else:
                position = analysis.slot_origin + Fraction(complete * 50, rate)
                track.progress = position.numerator // position.denominator
            for slot, powers in rows:
                self.analysed += 1
                if slot <= self.released[track.side]:
                    self.late += 1
                    continue
                self.pending[track.side].setdefault(slot, {})[identifier] = powers
            pending = self.pending[track.side]
            while len(pending) > 64:
                del pending[min(pending)]
                self.overflow += 1
            self._collect(now_ms)
            self.analysis_ns += time.perf_counter_ns() - started
            self.analysis_cpu_ns += time.thread_time_ns() - cpu_started

    def finish(self):
        with self.lock:
            self.finished = True
            try:
                self._collect(self.now_ms, force=True)
            except Exception as error:
                if self.problem is None:
                    self.problem = str(error)
                    LOG.exception("Could not finish the recording sound indicator")
            finally:
                self.tracks.clear()
                self.selected.clear()

    def _collect(self, now_ms, *, force=False):
        for side, pending in self.pending.items():
            if not pending:
                continue
            active = [
                track.progress
                for key, track in self.tracks.items()
                if key in self.selected
                and track.side == side
                and now_ms - track.arrival_ms <= min(1800, max(100, track.chunk_ms * 2.5))
            ]
            # A live source's progress seals earlier slots. A missing source
            # contributes nothing; it never freezes another participant's lane.
            cutoff = min(active) if active and not force else max(pending)
            for slot in sorted(key for key in pending if key <= cutoff):
                rows = tuple(pending.pop(slot).values())
                powers = rows[0] if len(rows) == 1 else [math.fsum(row[band] for row in rows) for band in range(32)]
                bands = self.quantizers[side].column(slot, powers)
                self.released[side] = slot
                column = (slot, 0, bands)
                for subscription in self.subscribers:
                    if len(subscription.lanes[side]) == 64:
                        self.overflow += 1
                    subscription.lanes[side].append(column)

    def drain(self, subscription, now_ms):
        with self.lock:
            if subscription not in self.subscribers:
                return {side: [] for side in self.pending}
            result = {}
            for side, queue in subscription.lanes.items():
                result[side] = list(queue)
                queue.clear()
            return result
