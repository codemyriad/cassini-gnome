"""D52.2 (P7) gap ranges and presence: literal vectors, oracle and mutation guards."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gaps', ROOT / 'conformance/gaps/gaps.py')
gaps = importlib.util.module_from_spec(spec); spec.loader.exec_module(gaps)
WRITER = json.loads((ROOT / 'conformance/gaps/writer.json').read_text())['cases']
PRESENCE = json.loads((ROOT / 'conformance/gaps/presence.json').read_text())['cases']


def matches(actual, expected):
    if (actual['state'], actual['problemKind']) != (expected['state'], expected['problemKind']):
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
    writer = (writer or gaps.Writer)(case.get('features', ()), **case.get('selection', {}))
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
                             ('flag-any-gap', AnyGapFlags), ('flag-from-stored-ranges', FlagFromStoredRanges)):
            with self.subTest(mutation=name):
                self.assertTrue(any(not matches(replay(c, mutant), c['expected']) for c in WRITER))

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
                track.update(sampleRate=48000, channels=1, frames=wanted['frames'], startOffsetMs=wanted['startOffsetMs'],
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


if __name__ == '__main__':
    unittest.main()
