# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 bounded, observer-owned analysis state. WIP: not wired into capture yet."""

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction

from .spectro_analysis import Quantizer, Track

LOG = logging.getLogger(__name__)


@dataclass(eq=False)
class Subscription:
    lanes: dict = field(default_factory=lambda: {side: deque(maxlen=64) for side in ("me", "others")})


@dataclass
class SourceTrack:
    analysis: Track
    side: str
    level: float
    arrival_ms: float
    chunk_ms: float = 20
    progress: int = -1


class SpectroSource:
    """Separate from the WAV lock, with one bounded output queue per observer.

    PCM belongs to its actual stored span ID and start offset. Only committed
    selected bytes may enter this API. Existing AC meters supply levels; the
    picture never supplies a permission, silence or recording decision.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.subscribers, self.tracks, self.retired = set(), {}, set()
        self.problem = None
        self.selected, self.next_selection_ms = set(), 0
        self.pending = {side: {} for side in ("me", "others")}
        self.quantizers = {side: Quantizer() for side in ("me", "others")}
        self.released = {side: -1 for side in ("me", "others")}
        self.calls = self.analysed = self.analysis_ns = self.late = self.overflow = 0

    @property
    def active(self):
        return bool(self.subscribers) and self.problem is None

    def observe(self, *args, **kwargs):
        try:
            self.feed(*args, **kwargs)
        except Exception as error:
            # A decorative picture must never turn accepted PCM into an audio
            # error, or prevent the next buffer from reaching the WAV writer.
            if self.problem is None:
                self.problem = str(error)
                LOG.exception("Recording sound indicator failed; saved audio is retained")

    def subscribe(self):
        with self.lock:
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
            self.tracks.pop(identifier, None)
            self.selected.discard(identifier)

    def _choose(self, now_ms):
        if now_ms < self.next_selection_ms:
            return
        self.next_selection_ms = now_ms + 1000
        self.selected = set()
        for side in self.pending:
            candidates = [(key, value) for key, value in self.tracks.items() if value.side == side]
            candidates.sort(key=lambda pair: (-pair[1].level, pair[0]))
            self.selected.update(key for key, _track in candidates[:3])

    def feed(self, identifier, data, offset_ms, *, rate, channels, kind, start_ms, now_ms, level, gap_frames=0):
        with self.lock:
            if not self.active or identifier in self.retired:
                return
            started = time.perf_counter_ns()
            self.calls += 1
            track = self.tracks.get(identifier)
            if track is None:
                if len(self.tracks) >= 64:
                    return
                side = "me" if kind in ("microphone", "local") else "others"
                track = self.tracks[identifier] = SourceTrack(
                    Track(rate, channels, start_ms=start_ms), side, level, now_ms
                )
                if sum(self.tracks[key].side == side for key in self.selected) < 3:
                    self.selected.add(identifier)
            track.level, track.arrival_ms = level, now_ms
            self._choose(now_ms)
            analysis = track.analysis
            expected_ms = start_ms + analysis.total * 1000 / rate
            missing = max(0, round((offset_ms - expected_ms) * rate / 1000))
            gap = max(gap_frames, missing)
            track.chunk_ms = len(data) * 1000 / (channels * 2 * rate)
            rows = analysis.feed(data, selected=identifier in self.selected, gap_frames=gap)
            complete = (analysis.total // analysis.hop) * analysis.hop
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
            self.analysis_ns += time.perf_counter_ns() - started

    def _collect(self, now_ms):
        for side, pending in self.pending.items():
            active = [
                track.progress
                for key, track in self.tracks.items()
                if key in self.selected
                and track.side == side
                and now_ms - track.arrival_ms <= max(100, track.chunk_ms * 2.5)
            ]
            if not pending:
                continue
            # A live source's progress seals earlier slots. A missing source
            # contributes nothing; it never freezes another participant's lane.
            cutoff = min(active) if active else max(pending)
            for slot in sorted(key for key in pending if key <= cutoff):
                rows = tuple(pending.pop(slot).values())
                powers = [math.fsum(row[band] for row in rows) for band in range(32)]
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
            self._collect(now_ms)
            result = {}
            for side, queue in subscription.lanes.items():
                result[side] = list(queue)
                queue.clear()
            return result
