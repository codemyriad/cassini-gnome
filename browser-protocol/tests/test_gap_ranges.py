"""D52.2 (P7) gap ranges and presence: literal vectors, oracle and mutation guards."""
import copy
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gaps', ROOT / 'conformance/gaps/gaps.py')
gaps = importlib.util.module_from_spec(spec); spec.loader.exec_module(gaps)
WRITER = json.loads((ROOT / 'conformance/gaps/writer.json').read_text())['cases']
PRESENCE = json.loads((ROOT / 'conformance/gaps/presence.json').read_text())['cases']


def matches(actual, expected):
    if (actual['state'], actual['problemKind']) != (expected['state'], expected['problemKind']):
        return False
    if actual['endReason'] != expected.get('endReason', 'user'):
        return False
    if 'problem' in expected and actual.get('problem') != expected['problem']:
        return False
    if len(actual['tracks']) != len(expected['tracks']):
        return False
    for track, wanted in zip(actual['tracks'], expected['tracks']):
        if any(track[k] != wanted[k] for k in ('wire', 'startOffsetMs', 'frames', 'gapFrames', 'droppedFrames')):
            return False
        if 'gaps' in wanted and track['gaps'] != wanted['gaps']:
            return False
        if 'gapsCount' in wanted and (len(track['gaps']) != wanted['gapsCount'] or track['gaps'][:2] != wanted['gapsHead']
                                      or track['gaps'][-2:] != wanted['gapsTail']):
            return False
    return True


def replay(case, writer=None):
    writer = (writer or gaps.Writer)(case.get('features', ()), problem=case.get('problem'), **case.get('selection', {}))
    for step in case['steps']:
        writer.step(step)
    return writer.result()


class Coalescing(gaps.Writer):
    def _pad(self, span, frames, kind):
        before = len(span['gaps'])
        super()._pad(span, frames, kind)
        if before and len(span['gaps']) > before and span['gaps'][-2]['kind'] == kind == 'jump':
            last = span['gaps'].pop()
            first = span['gaps'][-1]
            first['durationMs'] = last['atMs'] + last['durationMs'] - first['atMs']
            first['frames'] = round(first['durationMs'] * span['rate'] / 1000)


class Truncating(gaps.Writer):
    def _pad(self, span, frames, kind):
        if len(span['gaps']) < gaps.BUDGET:
            super()._pad(span, frames, kind)
        else:
            span['frames'] += frames
            span['gapFrames'] += frames


class CurrentSpanOnly(gaps.Writer):
    """The pre-D52.2 rule: replace the count on the live span, lost after deselection."""
    def trackEnd(self, id, droppedFrames=0):
        wire = self.wire[id]
        if wire['current'] is not None:
            wire['current']['droppedFrames'] = droppedFrames
        if wire['current'] is not None:
            wire['current']['ended'] = True
        wire['current'], wire['ended'] = None, True


class AnyGapFlags(gaps.Writer):
    def _pad(self, span, frames, kind):
        super()._pad(span, frames, kind)
        self.flagged = True


class FlagFromStoredRanges(gaps.Writer):
    def result(self):
        result = super().result()
        long = any(g['kind'] in ('jump', 'input') and g['frames'] >= 48000 for t in result['tracks'] for g in t['gaps'])
        return dict(result, state='interrupted' if long else 'completed', problemKind='gap' if long else None)


class WriterLossAsGap(gaps.Writer):
    """Host writer loss counted as missing input (the pre-D2 GNOME reading)."""
    def _pad(self, span, frames, kind):
        super()._pad(span, frames, 'input' if kind == 'writer' else kind)


class WriterLossKeepsUserEnd(gaps.Writer):
    def result(self):
        return dict(super().result(), endReason='user')


class WriterLossCoversNoHostTime(gaps.Writer):
    """Pads the lost frames on top of the whole host-time gap."""
    def input(self, id, hostMs, frames, writerFrames=0, inferredFrames=0):
        writerFrames += inferredFrames
        super().input(id, hostMs, frames)
        if writerFrames:
            span = self.native[id]['span']
            span['frames'] -= frames
            self._pad(span, writerFrames, 'writer')
            span['frames'] += frames


class InferredLossUnbounded(gaps.Writer):
    """Treats an estimate as an exact queue-dropped callback count."""
    def input(self, id, hostMs, frames, writerFrames=0, inferredFrames=0):
        super().input(id, hostMs, frames, writerFrames=writerFrames + inferredFrames)


class InferredTailUnbounded(gaps.Writer):
    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        super().writerTail(id, untilMs, resumeMs, writerFrames=inferredFrames if inferredFrames is not None else writerFrames)


class KnownLossCapped(gaps.Writer):
    """The f52f0e1 oracle shortens known lost callback frames to host time."""
    def input(self, id, hostMs, frames, writerFrames=0, inferredFrames=0):
        super().input(id, hostMs, frames, inferredFrames=writerFrames + inferredFrames)

    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        super().writerTail(id, untilMs, resumeMs, inferredFrames=writerFrames if writerFrames is not None else inferredFrames)


class WriterTailFromLastFrame(gaps.Writer):
    """Pads a tail loss from the last written frame, ignoring where the loss lies."""
    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        super().writerTail(id, untilMs)


class TailFromFilePosition(gaps.Writer):
    """#62 review: old oracle resurrects clock divergence absorbed between inputs."""
    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        span = self.native[id]['span']
        start, rate = Fraction(str(span['startOffsetMs'])), span['rate']
        until = gaps._half_up((Fraction(str(untilMs)) - start) * rate / 1000)
        frames = max(0, until - span['frames'])
        if resumeMs is not None:
            lost_from = gaps._half_up((Fraction(str(resumeMs)) - start) * rate / 1000) - (writerFrames if writerFrames is not None else inferredFrames)
            frames = min(frames, max(0, until - lost_from), writerFrames if writerFrames is not None else inferredFrames)
            if frames and lost_from - span['frames'] >= rate:
                self._pad(span, lost_from - span['frames'], 'input')
        if frames:
            self._pad(span, frames, 'writer')


class TailNeedsWriterOverlap(gaps.Writer):
    """#62 review: input before Stop disappears if writer loss is all after it."""
    def writerTail(self, id, untilMs, resumeMs=None, writerFrames=None, inferredFrames=None):
        rate = self.native[id]['span']['rate']
        if resumeMs is not None and Fraction(str(resumeMs)) - Fraction((writerFrames if writerFrames is not None else inferredFrames) * 1000, rate) >= Fraction(str(untilMs)):
            return
        super().writerTail(id, untilMs, resumeMs, writerFrames, inferredFrames)


class OldWriterProblem(gaps.Writer):
    def _loss_message(self, kind, milliseconds):
        return f"Recording saved with {gaps._half_up(milliseconds)} ms of audio lost during a disk stall."


class HalfEvenWriterProblem(gaps.Writer):
    def _loss_message(self, kind, milliseconds):
        return super()._loss_message(kind, Fraction(round(milliseconds)))


class PerRangeWriterProblem(gaps.Writer):
    def _writer_problems(self):
        return [self._loss_message(span['kind'], Fraction(gap['frames'] * 1000, span['rate']))
                for span in self.spans for gap in span['gaps'] if gap['kind'] == 'writer']


class LostEarlierProblem(gaps.Writer):
    def result(self):
        result = super().result()
        if self.problem:
            result['problem'] = result['problem'].removeprefix(self.problem + ' ')
        return result


class GapTests(unittest.TestCase):
    def test_oracle_reproduces_every_hand_written_writer_case(self):
        for case in WRITER:
            with self.subTest(case=case['name']):
                self.assertTrue(matches(replay(case), case['expected']), replay(case))

    def test_oracle_reproduces_every_hand_written_presence_case(self):
        for case in PRESENCE:
            with self.subTest(case=case['name']):
                self.assertEqual(gaps.presence(case['track'], case.get('ownership')), case['expected'])

    def test_mutations_fail_the_vectors(self):
        for name, mutant in (('coalesce', Coalescing), ('truncate', Truncating), ('current-span-drops', CurrentSpanOnly),
                             ('flag-any-gap', AnyGapFlags), ('flag-from-stored-ranges', FlagFromStoredRanges),
                             ('writer-loss-as-gap', WriterLossAsGap), ('writer-loss-user-end', WriterLossKeepsUserEnd),
                             ('writer-loss-covers-no-host-time', WriterLossCoversNoHostTime),
                             ('inferred-loss-unbounded', InferredLossUnbounded), ('known-loss-capped', KnownLossCapped),
                             ('writer-tail-from-last-frame', WriterTailFromLastFrame)):
            with self.subTest(mutation=name):
                self.assertTrue(any(not matches(replay(c, mutant), c['expected']) for c in WRITER))

    def test_review_tail_vectors_reject_previous_accounting(self):
        cases = {case['name']: case for case in WRITER}
        for name, mutant in (
            ('native-writer-tail-keeps-absorbed-positive-clock-divergence', TailFromFilePosition),
            ('native-writer-tail-keeps-absorbed-negative-clock-divergence', TailFromFilePosition),
            ('native-writer-tail-before-resume-keeps-absorbed-clock-divergence', TailFromFilePosition),
            ('native-writer-loss-starts-after-stop-but-input-gap-remains', TailNeedsWriterOverlap),
            ('native-writer-loss-starts-at-stop-but-input-gap-remains', TailNeedsWriterOverlap),
        ):
            with self.subTest(case=name, mutation=mutant.__name__):
                self.assertFalse(matches(replay(cases[name], mutant), cases[name]['expected']))

    def test_d69_1_known_and_inferred_loss_are_distinct(self):
        cases = {case['name']: case for case in WRITER}
        for name in ('mac-known-dropped-callback-keeps-all-frames-under-host-divergence',
                     'mac-known-dropped-tail-keeps-all-frames-past-stop',
                     'known-dropped-tail-without-following-buffer-is-exact'):
            with self.subTest(case=name):
                self.assertFalse(matches(replay(cases[name], KnownLossCapped), cases[name]['expected']))
        name = 'inferred-loss-under-the-same-host-divergence-is-clipped'
        self.assertFalse(matches(replay(cases[name], InferredLossUnbounded), cases[name]['expected']))
        name = 'inferred-tail-under-the-same-host-divergence-is-clipped'
        self.assertFalse(matches(replay(cases[name], InferredTailUnbounded), cases[name]['expected']))

    def test_shared_problem_literals_reject_incorrect_copy_rounding_and_aggregation(self):
        cases = {case['name']: case for case in WRITER}
        for name, mutant in (
            ('native-writer-loss-is-io-and-ends-in-error', OldWriterProblem),
            ('native-writer-problem-rounds-the-source-total-half-up-once', HalfEvenWriterProblem),
            ('native-writer-problem-rounds-the-source-total-half-up-once', PerRangeWriterProblem),
            ('repeated-native-writer-loss-reports-once-and-keeps-earlier-problem', PerRangeWriterProblem),
            ('repeated-native-writer-loss-reports-once-and-keeps-earlier-problem', LostEarlierProblem),
        ):
            with self.subTest(case=name, mutation=mutant.__name__):
                self.assertFalse(matches(replay(cases[name], mutant), cases[name]['expected']))

    def test_presence_starts_at_the_d55_placement(self):
        # R88 (D67 item 7): the raw fractional offset, or any other rounding of it, fails a literal.
        writedown = json.loads((ROOT / 'conformance/multistream/writedown.json').read_text())['keep']

        def failures():
            found = [c['name'] for c in PRESENCE if gaps.presence(c['track'], c.get('ownership')) != c['expected']]
            for case in writedown:
                ranges = gaps.presence(case['track'], case.get('ownership'))
                ranges = ranges['present'] + ranges['unknown']
                found += [case['name'] for word in case['words']
                          if any(a <= 8 * sum(word['ms']) < b for a, b in ranges) is not word['keep']]
            return found
        self.assertEqual(failures(), [])

        def exact(ms, rate):
            return gaps.Fraction(str(ms)) * rate / 1000
        for name, mutant in (('raw offset', exact), ('raw offset rounded up', lambda ms, rate: gaps.math.ceil(exact(ms, rate))),
                             ('half-even', lambda ms, rate: round(exact(ms, rate))),
                             ('floor', lambda ms, rate: gaps.math.floor(exact(ms, rate)))):
            with self.subTest(mutation=name), mock.patch.object(gaps, 'frame', mutant):
                self.assertNotEqual(failures(), [])
        with mock.patch.object(gaps, 'frame', exact):
            self.assertIn('a-fractional-start-offset-compares-in-session-samples', failures())

    def test_presence_never_comes_from_pcm_or_counts(self):
        for case in PRESENCE:
            track = dict(case['track'], droppedFrames=12345)
            self.assertEqual(gaps.presence(track, case.get('ownership')), case['expected'])
        widened = [c for c in PRESENCE if c['expected']['present'] != gaps.presence(dict(c['track'], gaps=[]), c.get('ownership'))['present']]
        self.assertGreaterEqual(len(widened), 5)

    def test_expected_tracks_validate_against_the_session_schema(self):
        schema = json.loads((ROOT / 'schemas/session.schema.json').read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        base = json.loads((ROOT / 'conformance/examples/session.json').read_text())
        for case in WRITER:
            expected = case['expected']
            for wanted in expected['tracks']:
                document = copy.deepcopy(base)
                track = document['tracks'][0]
                rate = next(s.get('rate', 48000) for s in case['steps']
                            if s['op'] in ('track', 'nativeTrack') and s['id'] == wanted['wire'])
                track.update(sampleRate=rate, channels=1, frames=wanted['frames'], startOffsetMs=wanted['startOffsetMs'],
                             gapFrames=wanted['gapFrames'], droppedFrames=wanted['droppedFrames'],
                             gaps=wanted.get('gaps', wanted.get('gapsHead', []) + wanted.get('gapsTail', [])))
                track.pop('endOffsetMs', None)
                with self.subTest(case=case['name'], wire=wanted['wire']):
                    validator.validate(document)
        document = copy.deepcopy(base)
        # Valid items, so only the budget can reject the longer list.
        item = dict(atMs=0, durationMs=1, frames=48, kind='jump')
        document['tracks'][0]['gaps'] = [item] * 1001
        self.assertTrue(validator.is_valid(document))
        document['tracks'][0]['gaps'] = [item] * 1002
        self.assertFalse(validator.is_valid(document))
        document['tracks'][0]['gaps'] = [dict(atMs=-1, durationMs=1, frames=48, kind='jump')]
        self.assertFalse(validator.is_valid(document))

    def test_an_empty_gap_validates_and_a_negative_one_fails(self):
        # D63 vectors and both apps read an empty gap (durationMs 0, frames 0) as no range.
        validator = Draft202012Validator(json.loads((ROOT / 'schemas/session.schema.json').read_text()),
                                         format_checker=FormatChecker())
        base = json.loads((ROOT / 'conformance/examples/session.json').read_text())

        def valid(gaps):
            document = copy.deepcopy(base)
            document['tracks'][0]['gaps'] = gaps
            return validator.is_valid(document)
        empty = dict(atMs=1000, durationMs=0, frames=0, kind='jump')
        self.assertTrue(valid([empty]))
        self.assertFalse(valid([dict(empty, durationMs=-1)]))
        self.assertFalse(valid([dict(empty, frames=-1)]))
        self.assertFalse(valid([dict(empty, durationMs=-1e-9, frames=0)]))
        # Both zero or both positive: a mixed entry is a writer bug, not a range.
        self.assertTrue(valid([dict(empty, durationMs=50, frames=2400)]))
        self.assertFalse(valid([dict(empty, durationMs=50, frames=0)]))
        self.assertFalse(valid([dict(empty, durationMs=0, frames=480)]))
        self.assertFalse(valid([dict(empty, durationMs=1e-9, frames=0)]))
        self.assertFalse(valid([dict(atMs=1000, kind='overflow', durationMs=0, frames=0)]))
        writedown = json.loads((ROOT / 'conformance/multistream/writedown.json').read_text())
        stored = [c['track']['gaps'] for c in PRESENCE + writedown['keep'] if c['track'].get('gaps')]
        stored += [c['gaps'] for c in writedown['turns'] if c.get('gaps')]
        self.assertIn([empty], stored)
        for gaps_ in stored:
            with self.subTest(gaps=gaps_):
                self.assertTrue(valid(gaps_))


if __name__ == '__main__':
    unittest.main()
