"""Code mutants of the presence and floor oracles must fail.

These patch the oracle functions, not the data: with a mutant in place, the same
hand-written literals that pass in test_multistream_corpus.py must fail. Two floor
rules cannot show in the corpus: stored gap ranges are zero-filled (D52.2) and so
inaudible anyway, and the corpus has no DC offset. Their mutants must fail the
synthetic oracle checks instead.
"""
import math
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest import mock

import test_multistream_corpus as guard

floor, gaps = guard.floor, guard.gaps


def _track(samples, start_ms=0, gap_list=()):
    return dict(startOffsetMs=start_ms, sampleRate=48000, frames=len(samples), gaps=list(gap_list))


def _db(amplitude):
    return 20 * math.log10(amplitude / 32768)


def _close(value, wanted):
    assert value is not None and abs(value - wanted) < 1e-6, (value, wanted)


def check_zero_padding_and_partial_frames():
    loud, quiet = [1000, -1000] * (floor.FRAME * 5), [10, -10] * (floor.FRAME * 5)
    samples = quiet + [0] * floor.FRAME * 10 + loud
    _close(floor.span(_track(samples), samples), _db(10))
    assert floor.span(_track([0] * floor.FRAME * 4), [0] * floor.FRAME * 4) is None


def check_gap_ranges_mask_frames():
    # A gap range over non-zero samples: presence removes the quiet part, leaving the loud frames.
    loud, quiet = [1000, -1000] * (floor.FRAME * 5), [10, -10] * (floor.FRAME * 5)
    samples = quiet + loud
    gap = dict(atMs=0, durationMs=len(quiet) / 48, frames=len(quiet), kind='jump')
    _close(floor.span(_track(samples, gap_list=[gap]), samples), _db(1000))


def check_ownership_limits_the_frames():
    samples = [10, -10] * (floor.FRAME * 5) + [1000, -1000] * (floor.FRAME * 5)
    half = len(samples) // 2 / 48
    _close(floor.span(_track(samples), samples, [[half, len(samples) / 48]]), _db(1000))


def check_mean_is_removed():
    # A DC offset of 5000 carrying a +-10 signal is a -70 dBFS floor, not -16 dBFS.
    samples = [5010, 4990] * (floor.FRAME * 5)
    _close(floor.span(_track(samples), samples), _db(10))


def check_person_is_the_minimum_known_span_floor():
    assert floor.person([-45.0, None, -80.0]) == -80.0
    assert floor.person([None]) is None


SYNTHETIC = [check_zero_padding_and_partial_frames, check_gap_ranges_mask_frames, check_ownership_limits_the_frames,
             check_mean_is_removed, check_person_is_the_minimum_known_span_floor]


class FloorOracleTests(unittest.TestCase):
    def test_synthetic_floor_rules(self):
        for check in SYNTHETIC:
            with self.subTest(check.__name__):
                check()

    def test_floor_mutants_the_corpus_cannot_show_fail_the_synthetic_rules(self):
        span = floor.span

        def levels_without_mean_removal(pcm, ranges):
            result = []
            for first, last in ranges:
                for start in range(-(-first // floor.FRAME) * floor.FRAME, last - floor.FRAME + 1, floor.FRAME):
                    energy = sum(x * x for x in pcm[start:start + floor.FRAME])
                    if energy > 0:
                        result.append(10 * math.log10(energy / floor.FRAME) - floor.FULL_SCALE_DB)
            return result
        mutants = {
            'span ignores gap ranges only': ('span', lambda track, pcm, ownership=None: span(dict(track, gaps=[]), pcm, ownership)),
            'span ignores ownership only': ('span', lambda track, pcm, ownership=None: span(track, pcm, None)),
            'levels without mean removal': ('levels', levels_without_mean_removal),
        }
        for check in SYNTHETIC:
            check()
        for name, (target, mutant) in mutants.items():
            with self.subTest(name), mock.patch.object(floor, target, mutant):
                failed = []
                for check in SYNTHETIC:
                    try:
                        check()
                    except AssertionError:
                        failed.append(check.__name__)
                self.assertTrue(failed, name + ' passes every synthetic floor rule')


class PresenceMutantTests(unittest.TestCase):
    def test_presence_mutants_fail_expected(self):
        data, _ = guard.corpus.load()
        guard.check_expected(data)
        original = gaps.presence
        mutants = {
            'ignores gap ranges': lambda track, ownership=None: original(dict(track, gaps=[]), ownership),
            'ignores ownership': lambda track, ownership=None: original(track),
        }
        for name, mutant in mutants.items():
            with self.subTest(name), mock.patch.object(gaps, 'presence', mutant):
                with self.assertRaises(AssertionError):
                    guard.check_expected(data)


class FloorMutantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, _ = guard.corpus.load()
        cls.temporary = tempfile.TemporaryDirectory()
        guard.corpus.make(Path(cls.temporary.name))
        cls.folders = {c['id']: Path(cls.temporary.name) / ('Recording-' + c['id']) for c in cls.data['sessions']}

    @classmethod
    def tearDownClass(cls):
        guard._PCM.clear()
        cls.temporary.cleanup()

    def test_floor_mutants_fail_expected(self):
        C = self.data['constants']
        guard.check_floors(self.data, self.folders, C)
        span = floor.span
        mutants = {
            'person pooled by max': ('person', lambda floors: max(f for f in floors if f is not None)),
            'person pooled by mean': ('person', lambda floors: statistics.fmean(f for f in floors if f is not None)),
            'span ignores presence': ('span', lambda track, pcm, ownership=None: span(dict(track, gaps=[]), pcm, None)),
            'span ignores ownership only': ('span', lambda track, pcm, ownership=None: span(track, pcm, None)),
        }
        for name, (target, mutant) in mutants.items():
            with self.subTest(name), mock.patch.object(floor, target, mutant):
                with self.assertRaises(AssertionError):
                    guard.check_floors(self.data, self.folders, C)
