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
from test_schemas import schema

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT/'conformance/control/resume-capture-vectors.json').read_text())

class ResumeTests(unittest.TestCase):
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

    def test_literal_resumed_seam_retains_mic_without_double_recording_the_others(self):
        spec = importlib.util.spec_from_file_location('resume_owner', ROOT/'conformance/multistream/ownership.py')
        owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
        seam = copy.deepcopy(VECTORS['seam'])
        actual = {track['id']:owner.windows(seam['events'],track) for track in seam['tracks']}
        self.assertEqual(actual,seam['expectedWindows'])
        for row in seam['ownershipAt']:
            self.assertEqual([identifier for identifier,windows in actual.items() if identifier != 'microphone'
                              and any(start <= row['atMs'] < end for start,end in windows)],row['others'])


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
