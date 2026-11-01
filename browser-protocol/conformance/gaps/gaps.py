"""Independent D52.2 (P7) oracle. Apps run the literal vectors through their own writers.

Writer: replays abstract wire/app steps (frame counts only, no PCM) and returns
each recorded span's gap facts plus the session flag. Presence: turns stored
track facts into integer 16 kHz session-sample ranges. Never reads PCM.
"""
import math
from fractions import Fraction

BUDGET = 1000
ME = ('local', 'microphone')


def _position(span):
    return Fraction(str(span['startOffsetMs'])) + Fraction(span['frames'] * 1000, span['rate'])


def _half_up(value):
    return math.floor(value + Fraction(1, 2))


def _number(value):
    return int(value) if value.denominator == 1 else float(value)


class Writer:
    def __init__(self, features=(), me=True, others=True):
        self.negotiated = 'source-outcomes-v1' in features
        self.me, self.others = me, others
        self.spans, self.wire, self.native = [], {}, {}
        self.flagged = False

    def _open(self, wire, kind, rate, boundary=None, native=False):
        span = dict(wire=wire, kind=kind, rate=rate, frames=0, gapFrames=0, droppedFrames=0, gaps=[],
                    startOffsetMs=boundary, boundary=boundary is not None, native=native, ended=False)
        self.spans.append(span)
        return span

    def _enabled(self, kind):
        return self.me if kind in ME else self.others

    def _pad(self, span, frames, kind):
        """One entry per padding write; never coalesced, never truncated silently."""
        at = _position(span)
        span['frames'] += frames
        if kind != 'prefix':
            span['gapFrames'] += frames
            self.flagged |= frames >= span['rate']
        gaps = span['gaps']
        if gaps and gaps[-1]['kind'] == 'overflow':
            return
        if len(gaps) == BUDGET:
            gaps.append(dict(atMs=_number(at), kind='overflow'))
            return
        gaps.append(dict(atMs=_number(at), durationMs=_number(Fraction(frames * 1000, span['rate'])),
                         frames=frames, kind=kind))

    def step(self, step):
        getattr(self, step['op'])(**{k: v for k, v in step.items() if k != 'op'})

    def track(self, id, kind='remote', rate=48000):
        wire = self.wire[id] = dict(kind=kind, rate=rate, current=None, last=None, ended=False)
        if self._enabled(kind):
            wire['current'] = wire['last'] = self._open(id, kind, rate)

    def audio(self, id, offsetMs, frames):
        span = self.wire[id]['current']
        if span is None:
            return  # deselected: discarded, not recorded
        if span['startOffsetMs'] is None:
            span['startOffsetMs'] = offsetMs
        delta = _half_up((Fraction(str(offsetMs)) - Fraction(str(span['startOffsetMs']))) * span['rate'] / 1000) - span['frames']
        if delta > span['rate'] // 50:
            self._pad(span, delta, 'prefix' if span['boundary'] and not span['frames'] else 'jump')
        span['frames'] += frames

    def series(self, id, firstOffsetMs, everyMs, count, frames):
        for index in range(count):
            self.audio(id, firstOffsetMs + index * everyMs, frames)

    def select(self, atMs, me=None, others=None):
        self.me = self.me if me is None else me
        self.others = self.others if others is None else others
        for identifier, wire in self.wire.items():
            enabled = self._enabled(wire['kind'])
            if wire['current'] is not None and not enabled:
                wire['current']['ended'], wire['current'] = True, None
            elif wire['current'] is None and enabled and not wire['ended']:
                wire['current'] = wire['last'] = self._open(identifier, wire['kind'], wire['rate'], atMs)

    def trackEnd(self, id, droppedFrames=0):
        wire = self.wire[id]
        if wire['last'] is not None:
            wire['last']['droppedFrames'] += droppedFrames  # +=, never =; no range
        if wire['current'] is not None:
            wire['current']['ended'] = True
        wire['current'], wire['ended'] = None, True

    def nativeTrack(self, id, kind='microphone', rate=48000, boundaryMs=None):
        self.native[id] = dict(span=self._open(id, kind, rate, boundaryMs, native=True), host=None, frames=0)

    def input(self, id, hostMs, frames):
        state = self.native[id]
        span = state['span']
        if state['host'] is None:
            if span['startOffsetMs'] is None:
                span['startOffsetMs'] = hostMs
            else:
                prefix = _half_up((Fraction(str(hostMs)) - Fraction(str(span['startOffsetMs']))) * span['rate'] / 1000)
                if prefix > 0:
                    self._pad(span, prefix, 'prefix')
        else:
            lag = (Fraction(str(hostMs)) - Fraction(str(state['host']))) * span['rate'] / 1000 - state['frames']
            gap = _half_up(lag)
            if gap >= span['rate']:
                self._pad(span, gap, 'input')
        state['host'], state['frames'] = hostMs, frames
        span['frames'] += frames

    def result(self):
        tracks = [dict(wire=s['wire'], startOffsetMs=s['startOffsetMs'], frames=s['frames'], gapFrames=s['gapFrames'],
                       droppedFrames=s['droppedFrames'], gaps=s['gaps']) for s in self.spans]
        return dict(state='interrupted' if self.flagged else 'completed',
                    problemKind='gap' if self.flagged else None, tracks=tracks)


def replay(case):
    writer = Writer(case.get('features', ()), **case.get('selection', {}))
    for step in case['steps']:
        writer.step(step)
    return writer.result()


EPSILON = Fraction(1, 1_000_000)


def _samples(left, right):
    # The epsilon absorbs floating-point noise in stored times (SPEC).
    return [math.ceil(Fraction(str(left)) * 16 - EPSILON), math.ceil(Fraction(str(right)) * 16 - EPSILON)]


def _subtract(windows, left, right):
    result = []
    for a, b in windows:
        if right <= a or b <= left:
            result.append((a, b))
            continue
        if a < left:
            result.append((a, left))
        if right < b:
            result.append((right, b))
    return result


def _intersect(windows, ownership):
    if ownership is None:
        return windows
    result = []
    for a, b in windows:
        for c, d in ownership:
            left, right = max(a, Fraction(str(c))), min(b, Fraction(str(d)))
            if left < right:
                result.append((left, right))
    return result


def presence(track, ownership=None):
    """Present and unknown half-open windows as 16 kHz session sample ranges."""
    start = Fraction(str(track['startOffsetMs']))
    extent = [(start, start + Fraction(track['frames'] * 1000, track['sampleRate']))]
    if track.get('gaps') is None:
        present, unknown = [], extent
    else:
        present, unknown = extent, []
        for gap in track['gaps']:
            at = Fraction(str(gap['atMs']))
            if gap['kind'] == 'overflow':
                unknown = [(max(a, at), b) for a, b in present if b > at]
                present = _subtract(present, at, extent[0][1])
                break
            present = _subtract(present, at, at + Fraction(str(gap['durationMs'])))
    return dict(present=[_samples(a, b) for a, b in _intersect(present, ownership)],
                unknown=[_samples(a, b) for a, b in _intersect(unknown, ownership)])
