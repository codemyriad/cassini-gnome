"""D52 corpus v2 guard: shape(), hand-written expected literals and the stored WAVs.

Every threshold comes from corpus.json `constants`; every expected value is a
literal in corpus.json `expected`. Ownership, presence, gaps, speech, overlaps and
floors are checked against the independent oracles (ownership.py, ../gaps/gaps.py,
floor.py), never against make.py; only the written session.json facts
(captureOutcome, droppedFrames) come from make.metadata(), the document under test.
"""
from array import array
import copy
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
MULTISTREAM = ROOT / 'conformance/multistream'


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


corpus = _module('corpus', MULTISTREAM / 'make.py')
floor = _module('floor', MULTISTREAM / 'floor.py')
gaps = floor.gaps  # one module object, so a patched gaps.presence reaches floor.span too
ownership = _module('ownership', MULTISTREAM / 'ownership.py')


# Half-open millisecond ranges.
def union(ranges):
    result = []
    for a, b in sorted(tuple(r[:2]) for r in ranges):
        if result and a <= result[-1][1]:
            result[-1][1] = max(result[-1][1], b)
        elif a < b:
            result.append([a, b])
    return result


def intersect(left, right):
    return union([max(a, c), min(b, d)] for a, b in union(left) for c, d in union(right) if max(a, c) < min(b, d))


def subtract(ranges, removed):
    result = union(ranges)
    for c, d in union(removed):
        result = [piece for a, b in result for piece in ([a, min(b, c)], [max(a, d), b]) if piece[0] < piece[1]]
    return result


def total(ranges):
    return sum(b - a for a, b in union(ranges))


def extent(track):
    return [[track['startMs'], track['endMs']]]


def speaker(track):
    return 'me' if track['kind'] in ('microphone', 'local') else 'others' if track['kind'] == 'system' else track['sourceTrackId']


def stored(track, rate=48000):
    return dict(startOffsetMs=track['startMs'], sampleRate=rate, frames=(track['endMs'] - track['startMs']) * rate // 1000,
                gaps=track.get('gaps', []))


def owned(case, track):
    return ownership.windows(case['events'], track)


def present_ms(case, track):
    """Owned presence in ms from the gaps oracle (16 kHz samples / 16)."""
    return [[a / 16, b / 16] for a, b in gaps.presence(stored(track), owned(case, track))['present']]


def speech(case):
    """Per person: speech windows inside the owned presence the oracle reports."""
    result = {}
    for track in case['tracks']:
        result.setdefault(speaker(track), []).extend(intersect(track['speechWindows'], present_ms(case, track)))
    return {key: union(value) for key, value in result.items()}


def overlaps(people):
    edges = sorted({edge for ranges in people.values() for r in ranges for edge in r})
    result = []
    for a, b in zip(edges, edges[1:]):
        talking = sorted(key for key, ranges in people.items() if intersect(ranges, [[a, b]]))
        if len(talking) < 2:
            continue
        if result and result[-1]['people'] == talking and result[-1]['ms'][1] == a:
            result[-1]['ms'][1] = b
        else:
            result.append(dict(ms=[a, b], people=talking))
    return result


def presence_windows(track):
    return track.get('presenceWindows', extent(track))


def spoken(track):
    """Where a track's own voice plays: its speech windows inside its presence."""
    return intersect([w[:2] for w in track['speechWindows']], presence_windows(track))


def carried(track, span):
    """What a track itself carries over `span`, by content key: voice and phase
    (phaseMs defaults to the track start), noise bed, and bleeds, each clipped to
    its presence. Two tracks with equal maps over a span hold the same call there."""
    present = intersect(span, presence_windows(track))
    items = {}
    for w in track['speechWindows']:
        key = ('speech', track['speech'], w[2] if len(w) > 2 else track['startMs'])
        items.setdefault(key, []).extend(intersect([w[:2]], present))
    if track.get('noise'):
        bed = track['noise']
        items.setdefault(('noise', bed['seed'], bed['originMs'], bed['dbfs']), []).extend(intersect(bed['windows'], present))
    for bleed in track.get('bleeds', []):
        items.setdefault(('bleed', bleed['from'], bleed['db']), []).extend(intersect(bleed['windows'], present))
    return {key: union(ranges) for key, ranges in items.items() if union(ranges)}


def seam(case, at, minimum):
    """System/browser pairs that physically overlap around `at` and carry the same call
    there (same voice, phase, noise and bleeds), with at least `minimum` ms of that
    call's speech. Returns (system id, browser id, overlap, shared speech ranges)."""
    result = []
    for system in (t for t in case['tracks'] if t['kind'] == 'system'):
        for browser in (t for t in case['tracks'] if 'sourceTrackId' in t):
            both = intersect(extent(system), extent(browser))
            if not both or not both[0][0] <= at <= both[0][1] or total(both) < minimum:
                continue
            call = carried(system, both)
            if call != carried(browser, both):
                continue
            talk = union([r for key, ranges in call.items() if key[0] == 'speech' for r in ranges])
            if total(talk) >= minimum:
                result.append((system['id'], browser['id'], both[0], talk))
    return result


def wall(case):
    start = datetime.fromisoformat(case['createdAt'].replace('Z', '+00:00')).timestamp() * 1000
    return [start, start + case['durationMs']]


def shape(data, C):
    """One assert per requirement, each with its own message; thresholds only from C."""
    case = data['sessions'][0]
    tracks = case['tracks']
    by_id = {t['id']: t for t in tracks}
    spans = {}
    for track in tracks:
        if 'sourceTrackId' in track:
            spans.setdefault(track['sourceTrackId'], []).append(track)

    assert any(min(t['startMs'] for t in group) >= C['LATE_REMOTE_MS'] for group in spans.values()), \
        'R1 late remote: no source starts its first span at or after LATE_REMOTE_MS'

    def density(track, ranges):
        return total(intersect(ranges, track.get('presenceWindows', extent(track)))) / total(extent(track))
    assert any(density(t, t.get('presenceWindows', extent(t))) < C['SPARSE_DENSITY_MAX'] and
               density(t, t['speechWindows']) < C['SPARSE_DENSITY_MAX'] for t in tracks), \
        'R2 sparse span: no span has presence and speech density below SPARSE_DENSITY_MAX'

    assert any(len(group) >= 2 and max(total(extent(t)) for t in group) >= C['SPAN_RATIO_MIN'] * min(total(extent(t)) for t in group)
               for group in spans.values()), 'R3 unequal spans: no source has two spans differing by SPAN_RATIO_MIN'

    sources = iter(e['source'] for e in case['events'] if e['type'] == 'othersSource')
    assert all(wanted in sources for wanted in ('browser', 'system', 'browser')), \
        'R4 source switch: no browser -> system -> browser sequence'
    connected = {e['atMs']: seam(case, e['atMs'], C['D49_OVERLAP_MS']) for e in case['events'] if e.get('reason') == 'callConnected'}
    assert connected and all(connected.values()), \
        'R4 D49 overlap: callConnected has no system/browser physical overlap carrying one call with D49_OVERLAP_MS of speech'
    assert all(any(total(intersect(talk, [[both[0], at]])) and total(intersect(talk, [[at, both[1]]]))
                   for _, _, both, talk in pairs) for at, pairs in connected.items()), \
        'R4 D49 straddle: no D49 overlap carries the call on both sides of callConnected'
    seams = [e['atMs'] for e in case['events'] if e.get('reason') in ('lost', 'restored')]
    assert seams and all(seam(case, at, C['SEAM_OVERLAP_MS']) for at in seams), \
        'R4 seams: a lost/restored seam has no physical overlap carrying one call with SEAM_OVERLAP_MS of speech'

    assert any(g['kind'] == 'jump' and g['frames'] >= data['rate'] and g['durationMs'] > 0 for t in tracks for g in t.get('gaps', [])), \
        'R5 counted gap: no jump gap range of at least one second of frames'
    for track in tracks:
        received = total(track.get('presenceWindows', extent(track)))
        missing = sum(g['durationMs'] for g in track.get('gaps', []))
        assert received + missing == total(extent(track)), 'R5 presence and gaps disagree: ' + track['id']
        # The D52.2 presence rule over the stored ranges reproduces the generator's windows.
        assert gaps.presence(stored(track))['present'] == [[a * 16, b * 16] for a, b in track.get('presenceWindows', extent(track))], \
            'R5 stored gaps disagree with presence: ' + track['id']
    assert any(t.get('droppedFrames', 0) > 0 for t in tracks), 'R5 droppedFrames: no track carries a producer drop count'

    for track in tracks:
        for b in track.get('bleeds', []):
            assert b['windows'] and not subtract(b['windows'], intersect(spoken(by_id[b['from']]), presence_windows(track))), \
                'R6 bleed windows: a bleed has no windows, or one where its source is silent or the target absent: ' + track['id']

    def bled(into, source):
        """A BLEED_DB bleed between those kinds, audible somewhere the target itself is quiet."""
        return any(b['db'] == C['BLEED_DB'] and into(t) and source(by_id[b['from']]) and total(subtract(b['windows'], spoken(t)))
                   for t in tracks for b in t.get('bleeds', []))
    mic = lambda t: t['kind'] in ('microphone', 'local')
    remote = lambda t: 'sourceTrackId' in t
    assert bled(remote, mic), 'R6 bleed mic into a remote: no BLEED_DB bleed of the microphone into a browser track'
    assert bled(mic, remote), 'R6 bleed remote into the mic: no BLEED_DB bleed of a browser track into the microphone'
    people = speech(case)
    assert any(total(intersect(people['me'], ranges)) >= C['OVERLAP_MIN_MS'] for key, ranges in people.items() if key != 'me'), \
        'R6 double-talk: my speech overlaps no other person by OVERLAP_MIN_MS'
    voices = {}
    for track in tracks:
        voices.setdefault(speaker(track), set()).add(data['speech'][track['speech']]['sha256'])
    assert all(len(v) == 1 for v in voices.values()), 'R6 one voice per person: a person speaks with two pinned voices'
    voice = {key: min(v) for key, v in voices.items()}
    remotes = [key for key in people if key != 'me']
    assert any(total(intersect(people[x], people[y])) >= C['OVERLAP_MIN_MS'] and voice[x] != voice[y]
               for x in remotes for y in remotes if x < y), \
        'R6 overlapping remotes: no two remote people with different voices overlap by OVERLAP_MIN_MS'
    assert len({data['speech'][t['speech']]['sha256'] for t in tracks}) >= C['VOICES_MIN'], \
        'R6 voices: fewer than VOICES_MIN distinct pinned voices'

    sessions = data['sessions']
    assert len({s['id'] for s in sessions}) >= 2, 'R7 sessions: fewer than two sessions with distinct ids'
    assert any(min(wall(x)[1], wall(y)[1]) - max(wall(x)[0], wall(y)[0]) >= C['CONCURRENT_MIN_MS']
               for i, x in enumerate(sessions) for y in sessions[i + 1:]), \
        'R7 concurrent folders: no two sessions overlap by CONCURRENT_MIN_MS of wall clock'


def check_expected(data):
    """The hand-written literals against the oracles; raises AssertionError on any difference."""
    expected = data['expected']
    for case in data['sessions']:
        literal = expected['sessions'][case['id']]
        assert set(literal['tracks']) == {t['id'] for t in case['tracks']}, case['id']
        for track in case['tracks']:
            wanted = literal['tracks'][track['id']]
            assert owned(case, track) == wanted['ownership'], ('ownership', track['id'])
            assert gaps.presence(stored(track), owned(case, track))['present'] == wanted['presence16k'], ('presence', track['id'])
            assert [[g['atMs'], g['atMs'] + g['durationMs']] for g in track.get('gaps', [])] == wanted['gaps'], ('gaps', track['id'])
            assert subtract(extent(track), present_ms(case, track)) == wanted['noWords'], ('noWords', track['id'])
        people = speech(case)
        assert {key: total(ranges) for key, ranges in people.items()} == literal['speechMs'], ('speechMs', case['id'])
        assert overlaps(people) == literal['overlaps'], ('overlaps', case['id'])
        assert {speaker(t) for t in case['tracks']} == set(literal['voices']) and \
            all(t['speech'] == literal['voices'][speaker(t)] for t in case['tracks']), ('voices', case['id'])
        connected = [union(p[2] for p in seam(case, e['atMs'], 1)) for e in case['events'] if e.get('reason') == 'callConnected']
        assert (connected[0][0] if connected and connected[0] else None) == literal['d49Overlap'], ('d49Overlap', case['id'])
        seams = [r for e in case['events'] if e.get('reason') in ('lost', 'restored') for r in union(p[2] for p in seam(case, e['atMs'], 1))]
        assert seams == literal['seamOverlaps'], ('seamOverlaps', case['id'])
        assert sorted([b['from'], t['id'], w] for t in case['tracks'] for b in t.get('bleeds', []) for w in b['windows']) == \
            sorted([b['from'], b['into'], b['ms']] for b in literal['bleeds']), ('bleeds', case['id'])
        beds = [dict(track=t['id'], ms=t['noise']['windows']) for t in case['tracks']
                if t.get('noise') and t['noise']['seed'] == t['id']]
        assert (beds[0] if beds else None) == literal['noiseBed'], ('noiseBed', case['id'])
        document = corpus.metadata(case)
        assert {s['sourceTrackId']: dict(captureOutcome=s['captureOutcome'], audioMs=s['audioMs'])
                for s in document['captureOutcome']['sources']} == literal['captureOutcome'], ('captureOutcome', case['id'])
        assert {t['id']: t['droppedFrames'] for t in document['tracks'] if 'droppedFrames' in t} == literal['droppedFrames'], \
            ('droppedFrames', case['id'])
    first, last = expected['concurrency']['sessions']
    cases = {case['id']: case for case in data['sessions']}
    window = [max(wall(cases[first])[0], wall(cases[last])[0]), min(wall(cases[first])[1], wall(cases[last])[1])]
    assert window == [wall(dict(createdAt=at, durationMs=0))[0] for at in expected['concurrency']['wallClock']], 'concurrency'


def floors(folder, case):
    """Oracle span and person floors over a materialized folder."""
    document = json.loads((folder / 'session.json').read_text())
    spans, people = {}, {}
    for track, meta in zip(case['tracks'], document['tracks']):
        spans[track['id']] = floor.span(meta, pcm(folder, meta['file']), owned(case, track))
        people.setdefault(speaker(track), []).append(spans[track['id']])
    return spans, {key: floor.person(values) for key, values in people.items()}


def check_floors(data, folders, C):
    for case in data['sessions']:
        literal = data['expected']['sessions'][case['id']]
        spans, people = floors(folders[case['id']], case)
        bands = [(track, spans[track], wanted['floorDb']) for track, wanted in literal['tracks'].items()]
        bands += [(key, people[key], band) for key, band in literal['personFloorDb'].items()]
        for name, value, (low, high) in bands:
            assert high - low == 2 * C['FLOOR_TOLERANCE_DB'], ('band width', name)
            assert value is not None and low <= value <= high, ('floor', name, value, low, high)


_PCM = {}


def pcm(folder, name):
    key = (str(folder), name)
    if key not in _PCM:
        with wave.open(str(folder / name)) as reader:
            samples = array('h', reader.readframes(reader.getnframes()))
        if sys.byteorder != 'little':
            samples.byteswap()
        _PCM[key] = samples
    return _PCM[key]


def _track(case, identifier):
    return next(t for t in case['tracks'] if t['id'] == identifier)


def _mutations():
    """One data mutation per shape() requirement, named by the message it must raise."""
    def first(data):
        return data['sessions'][0]

    def every(key):
        return lambda data: [t.pop(key, None) for t in first(data)['tracks']]

    def a_short_at_zero(data):
        track = _track(first(data), 'a-short')
        track.update(startMs=0, endMs=2000, speechWindows=[[0, 2000]])

    def drop_restored(data):
        first(data)['events'] = [e for e in first(data)['events'] if e.get('reason') != 'restored']

    def no_mic_overlap(data):
        mic = _track(first(data), 'mic')
        mic['speechWindows'] = [w for w in mic['speechWindows'] if w[0] != 60000]

    def b_ends_before_the_lost_seam(data):  # adversarial M1a: only a-long (C) still overlaps system-fallback
        _track(first(data), 'b').update(endMs=49000, speechWindows=[[18000, 28000, 17500]], presenceWindows=[[18000, 49000]])

    def b_return_starts_after_the_restored_seam(data):  # adversarial M1b: only a-return (C) still overlaps
        track = _track(first(data), 'b-return')
        track.update(startMs=61000, presenceWindows=[[61000, 70000], [72000, 90000]],
                     speechWindows=[[61000, 68000, 20000], [72000, 84000, 20000]])
        track['noise']['windows'] = [[61000, 70000], [72000, 90000]]
        _track(first(data), 'mic')['bleeds'][1]['windows'] = [[61000, 68000]]

    def phase(identifier, index, value):  # adversarial M4, M4b, M14, M21: one track out of phase
        return lambda data: _track(first(data), identifier)['speechWindows'][index].__setitem__(2, value)

    def bleed_windows(identifier, windows):  # adversarial M2, M3, M3b
        def mutate(data):
            for bleed in _track(first(data), identifier)['bleeds']:
                bleed['windows'] = windows
        return mutate

    def short_jumps(data):
        for track in first(data)['tracks']:
            for gap in track.get('gaps', []):
                if gap['kind'] == 'jump':
                    gap['frames'] = 47999

    return [
        ('R1 late remote', a_short_at_zero),  # audit probe: a-short moved to 0-2000
        ('R2 sparse span', lambda d: _track(first(d), 'a-long').update(presenceWindows=[[40000, 50000]])),
        ('R3 unequal spans', lambda d: _track(first(d), 'a-long').update(endMs=49999)),  # audit probe: 1 ms shorter
        ('R4 source switch', drop_restored),
        ('R4 D49 overlap', lambda d: _track(first(d), 'system').update(endMs=19000)),
        ('R4 D49 overlap', phase('system', 0, 17000)),
        ('R4 D49 overlap', phase('b', 0, 18000)),
        ('R4 D49 straddle', lambda d: _track(first(d), 'system').update(endMs=20000)),  # adversarial M5
        ('R4 seams', lambda d: _track(first(d), 'system-fallback').update(startMs=50000)),
        ('R4 seams', b_ends_before_the_lost_seam),
        ('R4 seams', b_return_starts_after_the_restored_seam),
        ('R4 seams', phase('system-fallback', 0, 48000)),
        ('R4 seams', phase('b-return', 0, 21000)),
        ('R4 seams', lambda d: _track(first(d), 'system-fallback')['noise'].update(seed='system-fallback')),  # M22
        ('R5 counted gap', short_jumps),
        ('R5 presence and gaps disagree', lambda d: _track(first(d), 'a-long').update(gaps=[])),
        ('R5 stored gaps disagree', lambda d: _track(first(d), 'b-return')['gaps'][0].update(atMs=71000)),
        ('R5 droppedFrames', every('droppedFrames')),
        ('R6 bleed windows', lambda d: [bleed_windows(t['id'], [])(d) for t in first(d)['tracks'] if t.get('bleeds')]),
        ('R6 bleed windows', bleed_windows('b', [[24000, 28000]])),  # my echo where I am silent (M3)
        ('R6 bleed windows', bleed_windows('b', [[38000, 46000]])),
        ('R6 bleed windows', lambda d: _track(first(d), 'mic')['bleeds'][0].update(windows=[[38000, 46000]])),  # b silent
        ('R6 bleed mic into a remote', lambda d: _track(first(d), 'b').pop('bleeds')),
        ('R6 bleed mic into a remote', lambda d: _track(first(d), 'b')['speechWindows'].append([32000, 38000])),  # echo masked
        ('R6 bleed remote into the mic', lambda d: _track(first(d), 'mic')['speechWindows'].append([20000, 28000])),
        ('R6 bleed remote into the mic', lambda d: [b.update(db=-30) for b in _track(first(d), 'mic')['bleeds']]),
        ('R6 double-talk', no_mic_overlap),  # audit probe: the mic's 60-68 s speech removed
        ('R6 one voice per person', lambda d: _track(first(d), 'a-long').update(speech='B')),  # adversarial M9
        ('R6 overlapping remotes', lambda d: [t.update(speech='B') for t in first(d)['tracks'] if t.get('sourceTrackId') == 'source-a']),
        ('R6 voices', lambda d: [t.update(speech='A') for t in first(d)['tracks'] if t.get('sourceTrackId') == 'source-a']),
        ('R7 sessions', lambda d: d['sessions'][1].update(id=d['sessions'][0]['id'])),
        ('R7 concurrent folders', lambda d: d['sessions'][1].update(createdAt='2026-10-31T12:01:20Z')),
    ]


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.data, self.speech = corpus.load()
        self.C = self.data['constants']

    def test_shape_and_real_pinned_speech(self):
        shape(self.data, self.C)
        # Loaded here so the rest of the guard (the digest pin included) runs on a bare
        # /usr/bin/python3; the session schema check needs jsonschema.
        annotations = _module('annotations', ROOT / 'conformance/check-session-events.py')
        for session in self.data['sessions']:
            annotations.validate(corpus.metadata(session))
        document = corpus.metadata(self.data['sessions'][0])
        self.assertEqual((document['state'], document['problemKind']), ('interrupted', 'gap'))
        self.assertTrue(all(isinstance(t['gaps'], list) for t in document['tracks']))
        self.assertEqual({t['id']: t['gapFrames'] for t in document['tracks'] if t['gapFrames']},
                         {'a-long': 336000, 'system-fallback': 72000, 'b-return': 96000})
        for track in document['tracks']:
            if 'sourceTrackId' in track:
                self.assertEqual((track['receivedFrames'], track['captureOutcome']), (track['frames'] - track['gapFrames'], 'captured'))
            else:
                self.assertNotIn('receivedFrames', track)
        self.assertTrue(all(any(samples) for samples in self.speech.values()))

    def test_every_shape_requirement_has_a_failing_mutation(self):
        for index, (message, mutate) in enumerate(_mutations()):
            with self.subTest(message, index=index):
                data = copy.deepcopy(self.data)
                mutate(data)
                with self.assertRaisesRegex(AssertionError, '^' + message):
                    shape(data, self.C)

    def test_expected_literals_match_the_oracles(self):
        check_expected(self.data)

    def test_source_overlap_mutations_are_rejected(self):
        for identifier, end in (('system', 65000), ('b', 90000)):
            with self.subTest(identifier):
                data = copy.deepcopy(self.data)
                _track(data['sessions'][0], identifier)['endMs'] = end
                with self.assertRaises(AssertionError):
                    check_expected(data)


class MaterializedCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.speech = corpus.load()
        cls.C = cls.data['constants']
        cls.temporary = tempfile.TemporaryDirectory()
        corpus.make(Path(cls.temporary.name))
        cls.folders = {case['id']: Path(cls.temporary.name) / ('Recording-' + case['id']) for case in cls.data['sessions']}
        cls.first = cls.folders[cls.data['sessions'][0]['id']]
        cls.second = cls.folders[cls.data['sessions'][1]['id']]

    @classmethod
    def tearDownClass(cls):
        _PCM.clear()
        cls.temporary.cleanup()

    def read(self, folder, identifier, left_ms, right_ms):
        """Samples of one stored track over session time [left_ms, right_ms)."""
        case = next(c for c in self.data['sessions'] if self.folders[c['id']] == folder)
        start = _track(case, identifier)['startMs']
        samples = pcm(folder, 'track-' + identifier + '.wav')[(left_ms - start) * 48:(right_ms - start) * 48]
        self.assertEqual(len(samples), (right_ms - left_ms) * 48)
        return samples

    def carrier(self, key, left_ms, right_ms, phase_ms):
        """The pinned 16 kHz voice, held for three frames and halved, read independently of make.py."""
        voice = self.speech[key]
        return [voice[((left_ms - phase_ms) * 48 + i) // 3 % len(voice)] / 2 for i in range((right_ms - left_ms) * 48)]

    def test_lengths_gaps_and_metadata(self):
        for case in self.data['sessions']:
            document = json.loads((self.folders[case['id']] / 'session.json').read_text())
            self.assertEqual(document, corpus.metadata(case))
            for meta in document['tracks']:
                with wave.open(str(self.folders[case['id']] / meta['file'])) as reader:
                    self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()),
                                     (48000, 1, 2, meta['frames']))
        for identifier, (left, right) in (('a-long', (41000, 48000)), ('b-return', (70000, 72000)),
                                          ('system-fallback', (54000, 55500))):
            self.assertFalse(any(self.read(self.first, identifier, left, right)), identifier)

    def test_output_is_byte_identical_to_the_pinned_digests(self):
        # make.py is stdlib only with integer positions and correctly rounded gains,
        # so Linux and macOS (/usr/bin/python3) must write these exact bytes.
        root = Path(self.temporary.name)
        for line in (MULTISTREAM / 'materialized.sha256').read_text().splitlines():
            digest, relative = line.split('  ', 1)
            self.assertEqual(hashlib.sha256((root / relative).read_bytes()).hexdigest(), digest, relative)
        self.assertEqual(len((MULTISTREAM / 'materialized.sha256').read_text().splitlines()),
                         sum(1 for path in root.rglob('*') if path.is_file()))

    def test_same_voice_in_two_independent_folders(self):
        # The same A carrier at the same offset in each file. Session 2 starts 20 s
        # later, so these samples are 20 s apart in wall time: identical content in
        # two independent files, not one shared timeline.
        mic = self.read(self.first, 'mic', 12000, 13000)
        self.assertEqual(mic, self.read(self.second, 'independent', 12000, 13000))
        self.assertTrue(any(mic))

    def own(self, case, identifier, left_ms, right_ms):
        """A track's own pinned voice over [left_ms, right_ms), from the carrier rule alone."""
        track = _track(case, identifier)
        self.assertFalse(track.get('noise'), identifier)
        out = [0.0] * ((right_ms - left_ms) * 48)
        for w in track['speechWindows']:
            for a, b in intersect(intersect([w[:2]], presence_windows(track)), [[left_ms, right_ms]]):
                out[(a - left_ms) * 48:(b - left_ms) * 48] = self.carrier(track['speech'], a, b, w[2] if len(w) > 2 else track['startMs'])
        return out

    def test_every_expected_bleed_is_stored(self):
        # Each expected bleed, including b-return into the mic under my own speech:
        # the stored target minus its own carrier is the stored source at BLEED_DB.
        C, case = self.C, self.data['sessions'][0]
        bleeds = self.data['expected']['sessions'][case['id']]['bleeds']
        self.assertEqual({(_track(case, b['from'])['kind'] == 'microphone', _track(case, b['into'])['kind'] == 'microphone')
                          for b in bleeds}, {(True, False), (False, True)})
        gain = 10 ** (C['BLEED_DB'] / 20)
        for bleed in bleeds:
            (left, right), into, source = bleed['ms'], bleed['into'], bleed['from']
            with self.subTest(into=into, source=source):
                echo = [x - o for x, o in zip(self.read(self.first, into, left, right), self.own(case, into, left, right))]
                original = self.read(self.first, source, left, right)
                ratio = sum(x * x for x in echo) / sum(x * x for x in original)
                self.assertAlmostEqual(10 * math.log10(ratio), C['BLEED_DB'], delta=0.1)
                self.assertLessEqual(max(abs(x - y * gain) for x, y in zip(echo, original)), 1)

    def test_three_voice_overlap_residue(self):
        # Mix the three physical tracks at 60-62 s, then subtract the pinned A (mic)
        # and C (a-return) carriers. What remains must be B's stored b-return, once
        # directly and once as its -35 dB bleed in the mic, within rounding.
        gain = 10 ** (self.C['BLEED_DB'] / 20)
        left, right = 60000, 62000
        mic, a, b = (self.read(self.first, i, left, right) for i in ('mic', 'a-return', 'b-return'))
        voice_a = self.carrier('A', left, right, 0)
        phase = _track(self.data['sessions'][0], 'a-return')['speechWindows'][0][2]
        voice_c = self.carrier('C', left, right, phase)
        residue = [m + x + y - p - q for m, x, y, p, q in zip(mic, a, b, voice_a, voice_c)]
        self.assertLessEqual(max(abs(r - (1 + gain) * y) for r, y in zip(residue, b)), 1.5)
        self.assertGreater(sum(y * y for y in b) / len(b), 1e4, 'B is not speaking in the overlap')
        wrong = self.carrier('B', left, right, phase)
        self.assertGreater(max(abs(x - w) for x, w in zip(a, wrong)), 1000, 'source-a carries B, not C')

    def test_d49_and_seam_overlaps_are_identical_in_both_tracks(self):
        literal = self.data['expected']['sessions'][self.data['sessions'][0]['id']]
        left, right = literal['d49Overlap']
        self.assertGreaterEqual(right - left, self.C['D49_OVERLAP_MS'])
        case = self.data['sessions'][0]
        pairs = [(one, other, both) for e in case['events'] if e.get('reason') in ('callConnected', 'lost', 'restored')
                 for one, other, both, _ in seam(case, e['atMs'], 1)]
        self.assertEqual([both for _, _, both in pairs], [literal['d49Overlap']] + literal['seamOverlaps'])
        for one, other, (left, right) in pairs:
            with self.subTest(one=one, other=other):
                samples = self.read(self.first, one, left, right)
                self.assertEqual(samples, self.read(self.first, other, left, right))
                self.assertTrue(any(samples))

    def test_noise_bed_level(self):
        for left, right in ((68000, 70000), (84000, 90000)):
            samples = self.read(self.first, 'b-return', left, right)
            level = 10 * math.log10(sum(x * x for x in samples) / len(samples) / 32768 ** 2)
            self.assertAlmostEqual(level, self.C['NOISE_BED_DBFS'], delta=0.1)

    def test_oracle_floors_are_inside_the_expected_bands(self):
        check_floors(self.data, self.folders, self.C)
