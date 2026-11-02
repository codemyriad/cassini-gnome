"""R67 schema/ownership vectors; desktop adapters also run actual native jobs."""
import copy
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
import uuid
from test_schemas import schema

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT/'conformance/control/resume-capture-vectors.json').read_text())

ATTRIBUTION = VECTORS['askAttribution']

def attribute(steps, ids=None):
    """Test-only ledger: a refusal answers the still-pending ask its requestId names."""
    ids = {} if ids is None else ids
    pending, times, entries, connections, closed = {}, {}, [], {}, set()
    def answer(request_id, message):
        if request_id in pending:
            ask = pending.pop(request_id)
            entries.append(dict(ask=ask, message=message, at=times[ask]))
    for at, step in enumerate(steps):
        op = step['op']
        if op == 'ask':
            assert step['connection'] not in closed, 'a closed connection never reopens'
            connections[step['ask']] = step['connection']
            ids.setdefault(step['ask'], str(uuid.uuid4()))
            times[step['ask']] = at; pending[ids[step['ask']]] = step['ask']
        elif op == 'reply':
            assert connections[step['ask']] not in closed, 'a reply needs its open connection'
            answer(ids[step['ask']], step['code'])  # R82 1b: the code is the message.
        elif op == 'close':
            closed.add(step['connection'])
        elif op == 'native':
            answer(ids[step['ask']], step['message'])
        elif op not in ('nativeWithoutId', 'nativeUnknownId'):
            raise AssertionError('unknown op ' + op)
        # A refusal without a requestId, or with one no ask was given, records nothing.
    return dict(entries=entries, pending=sorted(pending.values())), ids, times

class ResumeTests(unittest.TestCase):
    def test_ask_attribution_vectors_hold_with_a_fresh_id_per_ask(self):
        # R82 1b: one entry per ask, from its first answer, at its own time.
        self.assertGreaterEqual(len(ATTRIBUTION['cases']), 9)
        self.assertEqual(ATTRIBUTION['entry'], dict(side='browser', code='startRejected', detail='resumeRefused'))
        for case in ATTRIBUTION['cases']:
            with self.subTest(case=case['name']):
                actual, ids, times = attribute(case['steps'])
                self.assertEqual(len(set(ids.values())), len(ids))
                self.assertEqual([dict(ask=e['ask'], message=e['message']) for e in actual['entries']], case['expect']['entries'])
                self.assertTrue(all(e['at'] == times[e['ask']] for e in actual['entries']))
                self.assertEqual(actual['pending'], sorted(case['expect']['pending']))

    def test_a_recycled_request_id_breaks_the_finished_ask_vectors(self):
        # The codex case: A finished, B on a new connection reuses A's id, then
        # A's late native refusal would be written as B's entry.
        for name in ('finished-ask-late-native-never-answers-a-fresh-ask', 'finished-ask-late-native-then-the-fresh-ask-reply',
                     'finished-ask-late-native-never-answers-the-next-ask-on-its-connection'):
            with self.subTest(case=name):
                case = next(c for c in ATTRIBUTION['cases'] if c['name'] == name)
                shared = str(uuid.uuid4())
                recycled, _, _ = attribute(case['steps'], dict(A=shared, B=shared))
                self.assertNotEqual([dict(ask=e['ask'], message=e['message']) for e in recycled['entries']], case['expect']['entries'])

    def test_resume_start_and_explicit_proof_validate_without_changing_legacy_start(self):
        target = VECTORS['initial']
        request = dict(type='command',requestId='resume',action='start',mode='tracks',tabId=2,independent=True,
                       callGeneration=target['callGeneration'],resumeCapture={k:target[k] for k in ('captureId','timelineOriginMs')})
        schema('control-host').validate(request)
        for change in (dict(independent=False),dict(action='stop'),dict(callGeneration=None),dict(resumeCapture={})):
            self.assertFalse(schema('control-host').is_valid(dict(request,**change)))
        native = dict(type='resume',version=1,independent=True,features=['resume-capture-v1'],
                      **{k:target[k] for k in ('captureId','timelineOriginMs','callGeneration')})
        schema('request').validate(native)
        for field in ('captureId','timelineOriginMs','callGeneration','features','independent'):
            invalid = dict(native); invalid.pop(field)
            self.assertFalse(schema('request').is_valid(invalid),field)
        proof = dict(type='started',path='/test/stand-in',resumed=True,features=['resume-capture-v1'],
                     **{k:target[k] for k in ('captureId','timelineOriginMs','callGeneration')})
        schema('response').validate(proof)
        for field in ('captureId','timelineOriginMs','callGeneration','features'):
            invalid = dict(proof); invalid.pop(field)
            self.assertFalse(schema('response').is_valid(invalid),field)
        schema('request').validate(dict(type='start',version=1))
        schema('response').validate(dict(type='started',path='/fresh'))

    def test_resume_packet_may_carry_the_control_request_id(self):
        # 0.2.11: the ask's requestId, same domain as the control requestId.
        target = VECTORS['initial']
        native = dict(type='resume',version=1,independent=True,features=['resume-capture-v1'],
                      **{k:target[k] for k in ('captureId','timelineOriginMs','callGeneration')})
        for value in ('ask-A', 'é'*128, '\U0001F600'*128, 0, 9007199254740991):
            schema('request').validate(dict(native,requestId=value))
        for value in ('', 'é'*129, -1, 9007199254740992, 1.5, True, False, None, [], {}):
            self.assertFalse(schema('request').is_valid(dict(native,requestId=value)),repr(value))

    def test_spec_example_keeps_navigation_and_the_ask_waits_for_the_tab_release(self):
        # D57: the documented resume Start keeps the session across a reload,
        # and the app asks only once the extension released that tab.
        text = (ROOT/'SPEC-CONTROL.md').read_text()
        example = next(json.loads(line) for line in text.splitlines() if '"requestId":"join-stand-in"' in line)
        schema('control-host').validate(example)
        self.assertIs(example['keepOnNavigation'], True)
        self.assertIn('resumeCapture', example)
        self.assertIn("The app asks to resume only after the extension's status no longer reports a session for that tab.", text)

    def test_literal_resumed_seam_retains_mic_without_double_recording_the_others(self):
        spec = importlib.util.spec_from_file_location('resume_owner', ROOT/'conformance/multistream/ownership.py')
        owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
        seam = copy.deepcopy(VECTORS['seam'])
        actual = {track['id']:owner.windows(seam['events'],track) for track in seam['tracks']}
        self.assertEqual(actual,seam['expectedWindows'])
        for row in seam['ownershipAt']:
            self.assertEqual([identifier for identifier,windows in actual.items() if identifier != 'microphone'
                              and any(start <= row['atMs'] < end for start,end in windows)],row['others'])

    def test_lost_fallback_is_an_eligible_target_whose_seam_is_restored(self):
        # D52.4: a system/lost fallback resumes with browser/restored; only the
        # initial system/missing stand-in resumes with browser/callConnected.
        spec = importlib.util.spec_from_file_location('resume_owner', ROOT/'conformance/multistream/ownership.py')
        owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
        eligible = {(t['source'],t['reason']):t['seamReason'] for t in VECTORS['targets']['eligible']}
        self.assertEqual(eligible, {('system','missing'):'callConnected',('system','lost'):'restored'})
        self.assertFalse({(t['source'],t['reason']) for t in VECTORS['targets']['ineligible']} & set(eligible))
        for name in ('seam','lostSeam'):
            with self.subTest(seam=name):
                seam = copy.deepcopy(VECTORS[name])
                sources = [e for e in seam['events'] if e['type'] == 'othersSource']
                before, after = sources[-2], sources[-1]
                self.assertEqual((after['atMs'],after['source']),(seam['atMs'],'browser'))
                self.assertEqual(after['reason'],eligible[(before['source'],before['reason'])])
                actual = {track['id']:owner.windows(seam['events'],track) for track in seam['tracks']}
                self.assertEqual(actual,seam['expectedWindows'])
                for row in seam['ownershipAt']:
                    self.assertEqual([identifier for identifier,windows in actual.items() if identifier != 'microphone'
                                      and any(start <= row['atMs'] < end for start,end in windows)],row['others'])
        session = schema('session')
        self.assertTrue(all(session.is_valid(dict(json.loads((ROOT/'conformance/examples/session.json').read_text()),events=VECTORS[name]['events']))
                            for name in ('seam','lostSeam')))

    def test_native_host_without_the_active_target_refuses_before_creating_a_folder(self):
        target = VECTORS['initial']
        packet = dict(type='resume',version=1,independent=True,features=['resume-capture-v1'],
                      **{k:target[k] for k in ('captureId','timelineOriginMs','callGeneration')})
        payload = json.dumps(packet).encode()
        with tempfile.TemporaryDirectory() as private:
            result = subprocess.run([sys.executable,str(ROOT/'tests/browser_host_fixture.py'),private],
                                    input=struct.pack('=I',len(payload))+payload,capture_output=True,timeout=3,check=True)
            length, = struct.unpack('=I',result.stdout[:4])
            response = json.loads(result.stdout[4:4+length])
            self.assertEqual((response['type'],response['code'],response['scope']),('error','state','command'))
            self.assertEqual(list(Path(private).iterdir()),[])
