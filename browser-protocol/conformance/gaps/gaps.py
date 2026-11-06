"""Independent D52.2 (P7) oracle. Apps run the literal vectors through their own writers.

Writer: replays abstract wire/app steps (frame counts only, no PCM) and returns
each recorded span's gap facts plus the session flag. Presence: turns stored
track facts into integer 16 kHz session-sample ranges. A track's extent starts at its
D55 placement, never at the raw fractional offset. Never reads PCM.
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
    def __init__(self, features=(), me=True, others=True, problem=None):
        self.negotiated = 'source-outcomes-v1' in features
        self.me, self.others = me, others
        self.spans, self.wire, self.native = [], {}, {}
        self.flagged = self.lost = False
        self.problem = problem

    def _open(self, wire, kind, rate, boundary=None, native=False):
        span = dict(wire=wire, kind=kind, rate=rate, frames=0, gapFrames=0, droppedFrames=0, writerFrames=0, gaps=[],
                    startOffsetMs=boundary, boundary=boundary is not None, native=native, ended=False)
        self.spans.append(span)
        return span

    def _enabled(self, kind):
        return self.me if kind in ME else self.others

    def _pad(self, span, frames, kind):
        """One entry per padding write; never coalesced, never truncated silently."""
        at = _position(span)
        span['frames'] += frames
        if kind == 'writer':
            span['writerFrames'] += frames
            span['droppedFrames'] += frames  # host write loss: io, never the gap flag (R93-2 D2)
            self.lost = True
        elif kind != 'prefix':
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

    def input(self, id, hostMs, frames, writerFrames=0, inferredFrames=0):
        """Known callback loss is exact; inferred loss is a host-time estimate (D69.1)."""
        if writerFrames and inferredFrames:
            raise ValueError('Use known writerFrames or inferredFrames, not both')
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
            # Only inferred loss is capped. Observed queue-dropped callback frames
            # remain exact even when host/sample clock divergence was absorbed.
            writerFrames += min(inferredFrames, max(0, _half_up(lag)))
            gap = _half_up(lag - writerFrames)  # only host time the lost frames do not cover
            if gap >= span['rate']:
                self._pad(span, gap, 'input')
            if writerFrames:
                self._pad(span, writerFrames, 'writer')
        state['host'], state['frames'] = hostMs, frames
        span['frames'] += frames

    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        """Known admitted callback loss is exact, even past the Stop trigger.

        Without writerFrames, estimate unobserved loss from host time, clipped to
        untilMs. resumeMs/inferredFrames locate that estimated loss before a later
        buffer; uncovered host time ahead of it is an input gap.
        """
        if writerFrames is not None and inferredFrames is not None:
            raise ValueError('Use known writerFrames or inferredFrames, not both')
        state = self.native[id]
        span, rate = state['span'], state['span']['rate']
        # File time can differ from host time by clock divergence already absorbed.
        # Only new host time after the latest input's host end can be inferred.
        host_end = Fraction(str(state['host'])) + Fraction(state['frames'] * 1000, rate)
        until = max(0, _half_up((Fraction(str(untilMs)) - host_end) * rate / 1000))
        if writerFrames is not None:
            missing, lost = 0, writerFrames
            if resumeMs is not None:
                resume = _half_up((Fraction(str(resumeMs)) - host_end) * rate / 1000)
                missing = min(until, max(0, resume - writerFrames))
        elif resumeMs is None:
            missing, lost = 0, until
        else:
            resume = _half_up((Fraction(str(resumeMs)) - host_end) * rate / 1000)
            lost_from = max(0, resume - inferredFrames)
            missing = min(until, lost_from)
            lost = max(0, min(until, resume) - lost_from)
        # Uncovered input exists even when inferred loss starts at or beyond Stop.
        if missing >= rate:
            self._pad(span, missing, 'input')
        if lost:
            self._pad(span, lost, 'writer')

    def _loss_message(self, kind, milliseconds):
        return (f"{_half_up(milliseconds)} ms of {kind} audio was lost because the computer "
                "could not keep up; the gaps contain silence.")

    def _writer_problems(self):
        # Count only host writer padding, not producer drops or input gaps. Sum
        # exact durations before rounding once per source, in source creation order.
        totals = {}
        for span in self.spans:
            if span['writerFrames']:
                identity = span['wire']
                kind, duration = totals.get(identity, (span['kind'], Fraction(0)))
                totals[identity] = kind, duration + Fraction(span['writerFrames'] * 1000, span['rate'])
        return [self._loss_message(kind, duration) for kind, duration in totals.values()]

    def result(self):
        tracks = [dict(wire=s['wire'], startOffsetMs=s['startOffsetMs'], frames=s['frames'], gapFrames=s['gapFrames'],
                       droppedFrames=s['droppedFrames'], gaps=s['gaps']) for s in self.spans]
        return dict(state='interrupted' if self.flagged or self.lost or self.problem else 'completed',
                    problemKind='io' if self.lost else 'capture' if self.problem else 'gap' if self.flagged else None,
                    endReason='error' if self.lost else 'user', tracks=tracks,
                    problem=' '.join(filter(None, [self.problem, *self._writer_problems()])) or None)


def replay(case):
    writer = Writer(case.get('features', ()), problem=case.get('problem'), **case.get('selection', {}))
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


def frame(ms, rate):
    """D55: ms * rate / 1000 rounded half away from zero."""
    x = Fraction(str(ms)) * rate / 1000
    whole = math.floor(abs(x) + Fraction(1, 2))
    return whole if x >= 0 else -whole


def presence(track, ownership=None):
    """Present and unknown half-open windows as 16 kHz session sample ranges.

    The extent starts at the track's D55 placement, the 16 kHz session sample
    frame(startOffsetMs, 16000), and lasts frames/sampleRate from there. Gap ranges and
    ownership stay on the session timeline.
    """
    start = Fraction(frame(track['startOffsetMs'], 16000), 16)
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
            if gap['durationMs'] > 0:  # an empty gap is no range
                present = _subtract(present, at, at + Fraction(str(gap['durationMs'])))
    return dict(present=[_samples(a, b) for a, b in _intersect(present, ownership)],
                unknown=[_samples(a, b) for a, b in _intersect(unknown, ownership)])
