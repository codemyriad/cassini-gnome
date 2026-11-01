"""D52 shape guard: real speech, sparse spans, seams, overlap and two calls."""
import copy
import math
import struct
import tempfile
import wave
import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('corpus',ROOT/'conformance/multistream/make.py')
corpus=importlib.util.module_from_spec(spec);spec.loader.exec_module(corpus)
spec=importlib.util.spec_from_file_location('gaps',ROOT/'conformance/gaps/gaps.py')
gaps=importlib.util.module_from_spec(spec);spec.loader.exec_module(gaps)
spec=importlib.util.spec_from_file_location('annotations',ROOT/'conformance/check-session-events.py')
annotations=importlib.util.module_from_spec(spec);spec.loader.exec_module(annotations)

def shape(case):
    tracks=case['tracks']
    assert any(t.get('sourceTrackId') and t['startMs']>=30000 for t in tracks),'late remote missing'
    assert any(sum(b-a for a,b in t.get('presenceWindows',[]))<(t['endMs']-t['startMs'])/2 for t in tracks if 'presenceWindows' in t),'sparse span missing'
    spans=[t for t in tracks if t.get('sourceTrackId')=='source-a'];lengths=[t['endMs']-t['startMs'] for t in spans]
    assert len(spans)>=2 and max(lengths)>=5*min(lengths),'unequal repeated source spans missing'
    assert [e['reason'] for e in case['events'] if e['type']=='othersSource']==['missing','callConnected','lost','restored']
    assert any(g['kind']=='jump' and g['durationMs']>0 for t in tracks for g in t.get('gaps',[])),'counted gap missing'
    for track in tracks:
        if 'presenceWindows' in track:
            received=sum(b-a for a,b in track['presenceWindows'])
            missing=sum(g['durationMs'] for g in track.get('gaps',[]))
            assert received+missing==track['endMs']-track['startMs'],'presence and gaps disagree'
            # The D52.2 presence rule over the stored ranges reproduces the generator's windows.
            stored=dict(startOffsetMs=track['startMs'],sampleRate=48000,frames=(track['endMs']-track['startMs'])*48,gaps=track.get('gaps',[]))
            assert gaps.presence(stored)['present']==[[a*16,b*16] for a,b in track['presenceWindows']],'stored gaps disagree with presence'
    assert any(t.get('bleed',{}).get('db')==-35 for t in tracks),'bleed missing'

class CorpusTests(unittest.TestCase):
    def test_shape_and_real_pinned_speech(self):
        data,speech=corpus.load();self.assertEqual(len(data['sessions']),2)
        first=data['sessions'][0];shape(first)
        for session in data['sessions']:annotations.validate(corpus.metadata(session))
        document=corpus.metadata(first)
        self.assertEqual((document['state'],document['problemKind']),('interrupted','gap'))
        self.assertTrue(all(isinstance(t['gaps'],list) for t in document['tracks']))
        self.assertEqual({t['id']:t['gapFrames'] for t in document['tracks'] if t['gapFrames']},{'a-long':336000,'b-return':96000})
        self.assertTrue(all(any(samples) for samples in speech.values()))
        mic=first['tracks'][0];remote=first['tracks'][-1]
        self.assertTrue(any(corpus.sample(mic,t/48,speech) and corpus.sample(remote,t/48,speech) for t in range(60000*48,61000*48,173)))
        self.assertTrue(all(corpus.sample(remote,t,speech)==0 for t in (70000,71000,71999.99)))
    def test_presence_mask_mutation_fails_the_guard(self):
        data,_=corpus.load();case=copy.deepcopy(data['sessions'][0]);case['tracks'][3]['presenceWindows']=[[40000,50000]]
        with self.assertRaises(AssertionError):shape(case)
    def test_removing_a_gap_range_fails_the_guard(self):
        data,_=corpus.load();case=copy.deepcopy(data['sessions'][0]);case['tracks'][3]['gaps']=[]
        with self.assertRaises(AssertionError):shape(case)


def ownership(case):
    # Literal expected half-open ownership from the fixture's agreed S-6 seams.
    system = sorted((t['startMs'], t['endMs']) for t in case['tracks'] if t['kind'] == 'system')
    assert system == [(0, 20000), (50000, 60000)], 'system audio crosses a browser seam'
    for track in case['tracks']:
        if track['kind'] in ('microphone', 'system'):
            continue
        assert any(a <= track['startMs'] < track['endMs'] <= b for a, b in [(20000, 50000), (60000, 90000)]), 'browser audio crosses a system seam'


class MaterializedCorpusTests(unittest.TestCase):
    def test_actual_wavs_preserve_seams_gaps_overlap_and_bleed(self):
        data, speech = corpus.load()
        ownership(data['sessions'][0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus.make(root)
            first = root / ('Recording-' + data['sessions'][0]['id'])
            second = root / ('Recording-' + data['sessions'][1]['id'])
            expected = {'mic':4320000, 'system':960000, 'system-fallback':480000,
                        'a-short':96000, 'a-long':480000, 'a-return':96000,
                        'b':1440000, 'b-return':1440000}
            for identifier, count in expected.items():
                with wave.open(str(first / ('track-' + identifier + '.wav'))) as reader:
                    self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()), (48000, 1, 2, count))

            def read(folder, identifier, first_frame, count):
                with wave.open(str(folder / ('track-' + identifier + '.wav'))) as reader:
                    reader.setpos(first_frame)
                    result = reader.readframes(count)
                self.assertEqual(len(result), count * 2)
                return result

            self.assertEqual(read(first, 'a-long', 48000, 7 * 48000), bytes(7 * 96000))
            self.assertEqual(read(first, 'b-return', 10 * 48000, 2 * 48000), bytes(2 * 96000))
            # One voice in two genuinely independent files, at the same meeting time.
            mic = read(first, 'mic', 12 * 48000, 48000)
            self.assertEqual(mic, read(second, 'independent', 12 * 48000, 48000))
            self.assertTrue(any(mic))
            # The first B turn is alone in the mic, so its injected bleed can be
            # checked against the actual stored B waveform, not the generator.
            mic = struct.unpack('<48000h', read(first, 'mic', 20 * 48000, 48000))
            remote = struct.unpack('<48000h', read(first, 'b', 0, 48000))
            ratio = sum(x*x for x in mic) / sum(x*x for x in remote)
            self.assertAlmostEqual(10 * math.log10(ratio), -35, delta=0.1)
            # Independently remove the pinned A carrier from the overlap. The
            # residue must be B's actual resumed WAV at the same meeting time.
            mixed = struct.unpack('<48000h', read(first, 'mic', 60 * 48000, 48000))
            returned = struct.unpack('<48000h', read(first, 'b-return', 0, 48000))
            gain = 10 ** (-35 / 20)
            reference_a = speech['A']
            errors = [abs(value - reference_a[(60 * 16000 + i // 3) % len(reference_a)] / 2 - returned[i] * gain)
                      for i, value in enumerate(mixed)]
            self.assertLessEqual(max(errors), 1, 'overlapping B bleed is not aligned with its resumed source')

    def test_source_overlap_mutations_are_rejected(self):
        data, _ = corpus.load()
        case = copy.deepcopy(data['sessions'][0])
        case['tracks'][1]['endMs'] = 65000
        with self.assertRaises(AssertionError):
            ownership(case)
        case = copy.deepcopy(data['sessions'][0])
        next(t for t in case['tracks'] if t['id'] == 'b')['endMs'] = 90000
        with self.assertRaises(AssertionError):
            ownership(case)
