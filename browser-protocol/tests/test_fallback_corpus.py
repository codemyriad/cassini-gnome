"""D63 vectors: per-person fallback activity (#1), turn breaks at gap ranges (#2) and
the write-down presence rule (#3).

Every value checked here is a hand-written literal in corpus.json `expected`,
fallback.json or writedown.json. The literals are checked against the independent
oracles activity.py and writedown.py, which use ../gaps/gaps.py and ownership.py;
none of them is production code for either app. Code mutants of the oracles
patch the oracle functions: with one in place the same literals must fail.
"""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_multistream_corpus as guard

activity = guard._module('activity', guard.MULTISTREAM / 'activity.py')
writedown = guard._module('writedown', guard.MULTISTREAM / 'writedown.py')
FALLBACK = json.loads((guard.MULTISTREAM / 'fallback.json').read_text())
WRITEDOWN = json.loads((guard.MULTISTREAM / 'writedown.json').read_text())
STEP_MS = 100  # bands are the oracle at threshold +-ACTIVE_TOLERANCE_DB, floored/ceiled to 0.1 s


def _ms(seconds):
    return round(seconds * 1000)


def check_constants(C):
    assert (activity.MIN_ACTIVE_MS, activity.FEW_WORDS, activity.ACTIVE_DBFS, activity.FLOOR_MARGIN_DB,
            activity.FLOOR_MIN_FRAMES, activity.MIN_RUN_FRAMES * activity.FRAME_MS, activity.WINDOW // 16,
            activity.OVERLAP // 16, activity.MIN_WINDOW_FRAMES) == \
        (C['MIN_ACTIVE_SECONDS'] * 1000, C['FEW_WORDS'], C['ACTIVE_DBFS'], C['FLOOR_MARGIN_DB'], C['FLOOR_MIN_FRAMES'],
         C['MIN_RUN_MS'], C['WINDOW_MS'], C['OVERLAP_MS'], -(-C['MIN_WINDOW_ACTIVE_MS'] // activity.FRAME_MS)), 'constants'


def pooling(data, C):
    """Literal-level guard: the corpus must exercise pooling, with no undecided person."""
    least = C['MIN_ACTIVE_SECONDS']
    pooled_only = negative = False
    unsummed = []
    for case in data['sessions']:
        literal = data['expected']['sessions'][case['id']]
        bands = {}
        for track in case['tracks']:
            bands.setdefault(guard.speaker(track), []).append(literal['tracks'][track['id']]['activeSeconds'])
        assert set(bands) == set(literal['personActiveSeconds']) == set(literal['fallback']), \
            'D63 people: personActiveSeconds and fallback must list every speaker: ' + case['id']
        for person, spans in bands.items():
            low, high = literal['personActiveSeconds'][person]
            assert low >= least or high < least, 'D63 straddle: a pooled band straddles MIN_ACTIVE_SECONDS: ' + person
            assert literal['fallback'][person] is (low >= least), 'D63 fallback literal disagrees with its band: ' + person
            # An app whose track values lie in their bands must lie in the pooled band too.
            if [round(10 * low), round(10 * high)] != [sum(round(10 * v[0]) for v in spans), sum(round(10 * v[1]) for v in spans)]:
                unsummed.append(person)
            if len(spans) >= 2 and all(h < least for _, h in spans) and low >= least:
                pooled_only = True
            if len(spans) >= 2 and high < least:
                negative = True
    assert pooled_only, 'D63 pooling: no person with two or more spans falls back only when pooled'
    assert negative, 'D63 pooling: no person with two or more spans stays under MIN_ACTIVE_SECONDS pooled'
    assert not unsummed, "D63 pooled band: personActiveSeconds is not the sum of its tracks' activeSeconds bands: " + repr(unsummed)


class Measured:
    """Oracle active frames per track at the three thresholds, with levels cached."""

    def __init__(self, data, folders, C):
        self.data, self.folders, self.C = data, folders, C
        self.levels = {}

    def run(self, case, track, shift):
        folder = self.folders[case['id']]
        document = json.loads((folder / 'session.json').read_text())
        meta = next(t for t in document['tracks'] if t['id'] == track['id'])
        samples = guard.pcm(folder, meta['file'])
        key = (case['id'], track['id'])
        if key not in self.levels:
            self.levels[key] = activity.levels(samples)
        return meta, activity.measure(meta, samples, guard.owned(case, track), shift, self.levels[key])


def check_activity(data, folders, C):
    tolerance = C['ACTIVE_TOLERANCE_DB']
    measured = Measured(data, folders, C)
    for case in data['sessions']:
        literal = data['expected']['sessions'][case['id']]
        people = {}
        for track in case['tracks']:
            wanted = literal['tracks'][track['id']]
            low, high = (_ms(v) for v in wanted['activeSeconds'])
            meta, strict = measured.run(case, track, tolerance)
            _, loose = measured.run(case, track, -tolerance)
            _, middle = measured.run(case, track, 0.0)
            assert low <= strict['frames'] * activity.FRAME_MS < low + STEP_MS, ('activeSeconds low', track['id'], strict['frames'])
            assert high - STEP_MS < loose['frames'] * activity.FRAME_MS <= high, ('activeSeconds high', track['id'], loose['frames'])
            for result in (strict, middle, loose):
                assert activity.windows(meta, result['active'], guard.owned(case, track)) == wanted['fallbackWindowsMs'], \
                    ('fallbackWindowsMs', track['id'])
            pooled = people.setdefault(guard.speaker(track), [[], []])
            pooled[0].append(strict['frames'])
            pooled[1].append(loose['frames'])
        for person, (strict, loose) in people.items():
            low, high = (_ms(v) for v in literal['personActiveSeconds'][person])
            strict, loose = activity.person(strict), activity.person(loose)
            # The band is the sum of the track bands (pooling guard), so it holds the oracle's pooled values.
            assert low <= strict * activity.FRAME_MS, ('personActiveSeconds low', person, strict)
            assert loose * activity.FRAME_MS <= high, ('personActiveSeconds high', person, loose)
            # VAD rejecting everything: zero words on every track.
            assert activity.fires(strict, 0) == activity.fires(loose, 0) == literal['fallback'][person], ('fallback', person)


def check_writedown(data):
    for case in data['sessions']:
        literal = data['expected']['sessions'][case['id']]
        tracks = {t['id']: t for t in case['tracks']}
        for item in literal['wordMidpoints']:
            track = tracks[item['track']]
            got = writedown.keep(guard.stored(track), item['ms'], guard.owned(case, track))
            assert got is item['keep'], ('wordMidpoints', item)
        by_speaker = {}
        for track in case['tracks']:
            by_speaker.setdefault(guard.speaker(track), []).append(track)
        breaks = {key: [[a / 16, b / 16] for a, b in writedown.breaks([guard.stored(t) for t in spans])]
                  for key, spans in by_speaker.items()}
        assert breaks == literal['turnBreaksMs'], ('turnBreaksMs', case['id'])
        for key, spans in by_speaker.items():
            assert breaks[key] == guard.union(r for t in spans for r in literal['tracks'][t['id']]['gaps']), ('own gaps', key)
        for item in literal['turnCases']:
            spans = by_speaker[item['speaker']]
            # A turn case only uses words its speaker would write.
            for word in item['words']:
                assert any(writedown.keep(guard.stored(t), word, guard.owned(case, t)) for t in spans), ('unwritten word', item)
            got = writedown.turns(writedown.breaks([guard.stored(t) for t in spans]), item['words'])
            assert got == item['turns'], ('turnCases', item)


def check_seams(data):
    """A word at a seam is written by exactly one of the two tracks that carry the call."""
    case = data['sessions'][0]
    tracks = {t['id']: t for t in case['tracks']}
    pairs = [(one, other, e['atMs']) for e in case['events'] if e.get('reason') in ('callConnected', 'lost', 'restored')
             for one, other, _, _ in guard.seam(case, e['atMs'], 1)]
    assert len(pairs) == 3, pairs
    for one, other, at in pairs:
        for left in range(at - 600, at + 1, 50):
            for right in (at, at + 1, at + 9, at + 10, at + 11, at + 600):
                word = [left, right] if left <= right else [right, right]
                kept = [writedown.keep(guard.stored(tracks[t]), word, guard.owned(case, tracks[t])) for t in (one, other)]
                assert kept.count(True) == 1, ('seam', one, other, word, kept)


def check_synthetic_writedown(vectors):
    for case in vectors['keep']:
        for item in case['words']:
            assert writedown.keep(case['track'], item['ms'], case.get('ownership')) is item['keep'], (case['name'], item)
    for case in vectors['turns']:
        ranges = writedown.breaks([dict(gaps=case['gaps'])])
        assert writedown.turns(ranges, case['words']) == case['turns'], case['name']


def check_decisions(vectors):
    for case in vectors['cases']:
        verdict, entries = activity.decide(case['tracks'])
        assert verdict == case['expected']['fires'], ('fires', case['name'])
        assert entries == case['expected']['entries'], ('entries', case['name'])


# Synthetic activity checks: rules the corpus cannot show on its own.
def _meta(samples, gap_list=None):
    track = dict(startOffsetMs=0, sampleRate=48000, frames=len(samples))
    if gap_list is not None:
        track['gaps'] = gap_list
    return track


def _frames(amplitude, count):
    return [amplitude, -amplitude] * (activity.FRAME // 2) * count


def _active(samples, gap_list=None, ownership=None):
    return activity.measure(_meta(samples, gap_list), samples, ownership)['frames']


def check_run_length():
    quiet = _frames(0, 3)
    burst4 = (_frames(3000, 4) + quiet) * 10
    assert _active(burst4) == 0, 'four-frame bursts are clicks'
    assert _active(quiet + _frames(3000, 5) + quiet) == 5, 'a five-frame run is active'


def check_fixed_threshold_with_too_few_audible_frames():
    # A -48 dBFS tone over a -58 dBFS bed: active while -50 dBFS applies alone, not once
    # FLOOR_MIN_FRAMES audible frames give a floor (threshold -46 dBFS).
    few = _frames(41, activity.FLOOR_MIN_FRAMES - 101) + _frames(130, 100)
    assert _active(few) == 100, 'fewer than FLOOR_MIN_FRAMES audible frames: -50 dBFS alone'
    enough = _frames(41, activity.FLOOR_MIN_FRAMES - 100) + _frames(130, 100)
    assert _active(enough) == 0, 'with a floor, max(-50, floor + 12) applies'


def check_near_silence_never_lowers_the_floor():
    # Steady -45 dBFS noise is never active; +-1 frames (-90.3 dBFS) must not lower its floor.
    samples = _frames(1, 400) + _frames(184, 600)
    assert _active(samples) == 0, 'near-silence lowered the floor'


def check_mean_removal():
    samples = [5010, 4990] * (activity.FRAME // 2) * 20
    assert _active(samples) == 0, 'a DC offset is not activity'


def check_presence_and_edges():
    loud = _frames(3000, 20)
    gap = dict(atMs=0, durationMs=320, frames=activity.FRAME * 10, kind='jump')
    assert _active(loud, [gap]) == 10, 'a gap range over loud samples is not active'
    half = activity.FRAME // 2
    edge = dict(atMs=0, durationMs=half / 48, frames=half, kind='jump')
    assert _active(loud, [edge]) == 19, 'a frame straddling a presence edge is not inside'
    assert _active(loud, None, [[0, 320]]) == 10, 'ownership limits activity even when presence is unknown'


def check_windows():
    # 40 s of loud frames with a 2 s gap at 20 s: windows are laid per presence range.
    frames = 40000 // activity.FRAME_MS
    samples = (_frames(3000, 9) + _frames(3, 1)) * (frames // 10)
    gap = dict(atMs=20000, durationMs=2000, frames=96000, kind='jump')
    meta = _meta(samples, [gap])
    result = activity.measure(meta, samples)
    assert activity.windows(meta, result['active']) == [[0, 15000], [14500, 20000], [22000, 37000], [36500, 40000]], 'windows'
    # Frames 453-468 are loud; frame 468 (14976-15008 ms) is a partial edge frame of the
    # first window and still counts, so both windows hold MIN_WINDOW_FRAMES active frames.
    for loud, wanted in ((activity.MIN_WINDOW_FRAMES, [[0, 15000], [14500, 27808]]), (activity.MIN_WINDOW_FRAMES - 1, [])):
        samples = _frames(3, 453) + _frames(3000, loud) + _frames(3, 416 - loud)
        meta = _meta(samples)
        result = activity.measure(meta, samples)
        assert result['frames'] == loud
        assert activity.windows(meta, result['active']) == wanted, ('MIN_WINDOW_FRAMES with a partial edge frame', loud)


SYNTHETIC = [check_run_length, check_fixed_threshold_with_too_few_audible_frames, check_near_silence_never_lowers_the_floor,
             check_mean_removal, check_presence_and_edges, check_windows]


def _failures(checks):
    failed = []
    for check in checks:
        try:
            check()
        except AssertionError:
            failed.append(getattr(check, '__name__', repr(check)))
    return failed


def _pooling_mutations():
    def no_rejoined_guest(data):
        data['sessions'] = data['sessions'][:2]
        data['expected']['sessions'].pop('05200000-0000-4000-8000-000000000003')

    def literal(session, path, value):
        def mutate(data):
            target = data['expected']['sessions'][session]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return mutate
    first, third = '05200000-0000-4000-8000-000000000001', '05200000-0000-4000-8000-000000000003'
    return [
        ('D63 pooling: no person with two or more spans falls back only', no_rejoined_guest),
        ('D63 pooling: no person with two or more spans falls back only', literal(third, ['tracks', 'd-return', 'activeSeconds'], [15.0, 16.0])),
        ('D63 straddle', literal(first, ['personActiveSeconds', 'source-b'], [9.6, 15.1])),
        ('D63 fallback literal', literal(first, ['fallback', 'me'], False)),
        ('D63 people', lambda d: d['expected']['sessions'][third]['fallback'].pop('me')),
        ('D63 pooled band', literal(third, ['personActiveSeconds', 'source-d'], [19.2, 21.7])),
        ('D63 pooling: no person with two or more spans stays under',
         lambda d: [d['expected']['sessions'][first]['personActiveSeconds'].update({k: [15.0, 16.0]}) or
                    d['expected']['sessions'][first]['fallback'].update({k: True}) for k in ('others', 'source-a', 'source-b')]),
    ]


class VectorTests(unittest.TestCase):
    def setUp(self):
        self.data, _ = guard.corpus.load()
        self.C = self.data['constants']

    def test_constants_match_the_oracle(self):
        check_constants(self.C)

    def test_pooling_guard(self):
        pooling(self.data, self.C)

    def test_every_pooling_requirement_has_a_failing_mutation(self):
        for index, (message, mutate) in enumerate(_pooling_mutations()):
            with self.subTest(message, index=index):
                data = copy.deepcopy(self.data)
                mutate(data)
                with self.assertRaisesRegex(AssertionError, '^' + message):
                    pooling(data, self.C)

    def test_word_midpoints_turn_breaks_and_seams(self):
        check_writedown(self.data)
        check_seams(self.data)
        check_synthetic_writedown(WRITEDOWN)

    def test_decision_vectors(self):
        check_decisions(FALLBACK)

    def test_synthetic_activity_rules(self):
        self.assertEqual(_failures(SYNTHETIC), [])

    def test_writedown_mutants_fail(self):
        keep, breaks = writedown.keep, writedown.breaks

        def keep_half_up(track, word, ownership=None):  # midpoint rounded to whole ms, half up
            found = writedown.gaps.presence(track, ownership)
            m = (word[0] + word[1] + 1) // 2 * 16
            return any(a <= m < b for a, b in found['present'] + found['unknown'])

        def keep_closed(track, word, ownership=None):
            found = writedown.gaps.presence(track, ownership)
            m = writedown.midpoint(word)
            return any(a <= m <= b for a, b in found['present'] + found['unknown'])

        def keep_present_only(track, word, ownership=None):
            m = writedown.midpoint(word)
            return any(a <= m < b for a, b in writedown.gaps.presence(track, ownership)['present'])

        def keep_track_relative(track, word, ownership=None):  # ranges converted after subtracting the origin
            origin = writedown.Fraction(str(track['startOffsetMs']))
            shifted = dict(track, startOffsetMs=0)
            if track.get('gaps') is not None:
                shifted['gaps'] = [dict(g, atMs=writedown.Fraction(str(g['atMs'])) - origin) for g in track['gaps']]
            owned = None if ownership is None else [[writedown.Fraction(str(a)) - origin, writedown.Fraction(str(b)) - origin]
                                                    for a, b in ownership]
            found = writedown.gaps.presence(shifted, owned)
            m = writedown.midpoint(word) - 16 * origin
            return any(a <= m < b for a, b in found['present'] + found['unknown'])

        def split_closed(ranges, first, second):
            m1, m2 = writedown.midpoint(first), writedown.midpoint(second)
            return any(g0 <= max(m1, m2) and min(m1, m2) <= g1 for g0, g1 in ranges)

        def split_on_edges(ranges, first, second):
            return any(g0 < 16 * second[0] and 16 * first[1] < g1 for g0, g1 in ranges)

        def breaks_with_overflow(tracks):  # an overflow marker read as a gap to the end
            extra = [[writedown.gaps._samples(g['atMs'], g['atMs'])[0], 10 ** 12]
                     for t in tracks for g in t.get('gaps') or [] if g['kind'] == 'overflow']
            return sorted(breaks(tracks) + extra)
        mutants = {
            'midpoint rounded half up': ('keep', keep_half_up),
            'closed presence range': ('keep', keep_closed),
            'unknown presence dropped': ('keep', keep_present_only),
            'ownership ignored': ('keep', lambda track, word, ownership=None: keep(track, word)),
            'presence compared track-relative': ('keep', keep_track_relative),
            'gap ranges ignored': ('keep', lambda track, word, ownership=None: keep(dict(track, gaps=[]), word, ownership)),
            'turns never split': ('split', lambda ranges, first, second: False),
            'turns split on word edges': ('split', split_on_edges),
            'gap range closed at its end': ('split', split_closed),
            'an empty gap splits': ('split', lambda ranges, first, second: any(
                g0 < max(writedown.midpoint(first), writedown.midpoint(second))
                and min(writedown.midpoint(first), writedown.midpoint(second)) < g1 for g0, g1 in ranges)),
            'overflow splits': ('breaks', breaks_with_overflow),
            'other speakers gaps split': ('breaks', lambda tracks: breaks(tracks) + [[656000, 768000], [1120000, 1152000], [864000, 888000]]),
        }

        def all_writedown():
            check_writedown(self.data)
            check_seams(self.data)
            check_synthetic_writedown(WRITEDOWN)
        all_writedown()
        for name, (target, mutant) in mutants.items():
            with self.subTest(name), mock.patch.object(writedown, target, mutant):
                with self.assertRaises(AssertionError):
                    all_writedown()

    def test_decision_mutants_fail(self):
        need = activity.need
        mutants = {
            'per-track, not pooled': ('person', lambda frames: max(frames, default=0)),
            'ten words fire': ('fires', lambda frames, words: words <= activity.FEW_WORDS and frames * activity.FRAME_MS >= activity.MIN_ACTIVE_MS),
            'seconds rounded to 0.1 s first': ('fires', lambda frames, words: words < activity.FEW_WORDS and round(frames * 0.032, 1) >= 15),
            'margin without the minimum of two': ('need', lambda vad: 1 if vad == 0 else vad + -(-vad // 5)),
            'margin dropped': ('need', lambda vad: max(1, vad)),
        }
        check_decisions(FALLBACK)
        for name, (target, mutant) in mutants.items():
            with self.subTest(name), mock.patch.object(activity, target, mutant):
                with self.assertRaises(AssertionError):
                    check_decisions(FALLBACK)
        original = activity.decide

        def silent_counted(tracks):
            return original([dict(t, silent=False) for t in tracks])

        def pooled_margin(tracks):
            verdict, entries = original(tracks)
            total = sum(e['vadWords'] for e in entries)
            return verdict, [dict(e, used=e['fallbackWords'] >= need(total)) for e in entries]
        for name, mutant in {'silent tracks counted': silent_counted, 'margin pooled': pooled_margin}.items():
            with self.subTest(name), mock.patch.object(activity, 'decide', mutant):
                with self.assertRaises(AssertionError):
                    check_decisions(FALLBACK)


class MaterializedActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, _ = guard.corpus.load()
        cls.C = cls.data['constants']
        cls.temporary = tempfile.TemporaryDirectory()
        guard.corpus.make(Path(cls.temporary.name))
        cls.folders = {c['id']: Path(cls.temporary.name) / ('Recording-' + c['id']) for c in cls.data['sessions']}

    @classmethod
    def tearDownClass(cls):
        guard._PCM.clear()
        cls.temporary.cleanup()

    def test_oracle_activity_matches_the_bands_windows_and_fallback(self):
        check_activity(self.data, self.folders, self.C)

    def test_activity_mutants_fail(self):
        ranges16, runs = activity.ranges16, activity.runs
        code = {
            'per-track instead of pooled': ('person', lambda frames: max(frames, default=0)),
            'presence ignored': ('ranges16', lambda track, ownership=None: [[0, track['frames'] * 16000 // track['sampleRate']]]),
            'ownership ignored': ('ranges16', lambda track, ownership=None: ranges16(track, None)),
            'gap ranges ignored': ('ranges16', lambda track, ownership=None: ranges16(dict(track, gaps=[]), ownership)),
            'no run-length filter': ('runs', lambda loud: list(loud)),
            'floor margin dropped': ('FLOOR_MARGIN_DB', 0.0),
            'floor without the 10 s minimum': ('FLOOR_MIN_FRAMES', 0),
        }
        for name, (target, mutant) in code.items():
            with self.subTest(name), mock.patch.object(activity, target, mutant):
                with self.assertRaises(AssertionError):
                    check_activity(self.data, self.folders, self.C)
        self.assertIs(activity.runs, runs)

    def test_activity_mutants_the_corpus_cannot_show_fail_the_synthetic_rules(self):
        def level_without_mean_removal(frame):
            energy = sum(x * x for x in frame)
            return None if energy <= 0 else 10 * math.log10(energy / len(frame)) - activity.FULL_SCALE_DB
        mutants = {
            'near-silence counted as audible': ('AUDIBLE_DBFS', -1000.0),
            'no run-length filter': ('runs', lambda loud: list(loud)),
            'mean not removed': ('level', level_without_mean_removal),
        }
        self.assertEqual(_failures(SYNTHETIC), [])
        for name, (target, mutant) in mutants.items():
            with self.subTest(name), mock.patch.object(activity, target, mutant):
                self.assertTrue(_failures(SYNTHETIC), name + ' passes every synthetic activity rule')


class CorpusDataMutantTests(unittest.TestCase):
    """Generator changes, with the literals unchanged, must fail the activity check."""

    def mutated(self, mutate):
        data, speech = guard.corpus.load()
        mutate(data)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(guard.corpus, 'load', lambda: (copy.deepcopy(data), speech)):
            guard.corpus.make(Path(root))
            folders = {c['id']: Path(root) / ('Recording-' + c['id']) for c in data['sessions']}
            try:
                with self.assertRaises(AssertionError):
                    check_activity(data, folders, data['constants'])
            finally:
                guard._PCM.clear()

    def test_b_return_back_to_84_s(self):
        # Corpus v2's b-return: source-b's pooled band reaches 15 s, so the decision is undecided.
        self.mutated(lambda d: guard._track(d['sessions'][0], 'b-return')['speechWindows'].__setitem__(1, [72000, 84000, 20000]))

    def test_d_return_without_its_gap(self):
        def mutate(data):
            track = guard._track(data['sessions'][2], 'd-return')
            track.update(gaps=[], presenceWindows=[[24000, 50000]])
        self.mutated(mutate)


if __name__ == '__main__':
    unittest.main()
