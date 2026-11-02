import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('control_runner', ROOT / 'conformance/control/run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class ControlTests(unittest.TestCase):
    def test_schemas_and_both_runners(self):
        for name in ['extension', 'host']:
            Draft202012Validator.check_schema(json.loads((ROOT / f'schemas/control-{name}.schema.json').read_text()))
        cases = json.loads((ROOT / 'conformance/control/vectors.json').read_text())
        fixture = ROOT / 'conformance/control/fixture-host.py'
        with contextlib.redirect_stdout(io.StringIO()):
            for case in cases:
                with self.subTest(case=case['name']): self.assertTrue(runner.run(fixture, case))
        # Envelopes are schema-checked and exact-match compared in the host runner;
        # extension semantics and the same corpus are exercised by the VM runner.
        subprocess.run(['node', str(ROOT / 'conformance/control/run-extension.cjs')], check=True, stdout=subprocess.DEVNULL)

    def test_call_evidence_is_optional_and_typed_for_legacy_readers(self):
        validator=Draft202012Validator(json.loads((ROOT/'schemas/control-extension.schema.json').read_text()))
        row=dict(tabId=2,title='Pre-join fixture',url='https://meet.example/call',sourceCount=3)
        self.assertTrue(validator.is_valid(dict(type='meetings',meetings=[row])))
        for heard in [True,False]:
            self.assertTrue(validator.is_valid(dict(type='meetings',meetings=[dict(row,heard=heard)])))
        for heard in ['true',1,None]:
            self.assertFalse(validator.is_valid(dict(type='meetings',meetings=[dict(row,heard=heard)])))
        self.assertTrue(validator.is_valid(dict(type='hello',version=1,capabilities=['call-evidence-v1'])))

    def test_shared_desktop_auto_start_inputs_are_wire_valid(self):
        validator=Draft202012Validator(json.loads((ROOT/'schemas/control-extension.schema.json').read_text()))
        cases=json.loads((ROOT/'conformance/control/auto-start-vectors.json').read_text())
        self.assertEqual(len({case['name'] for case in cases}),len(cases))
        for case in cases:
            with self.subTest(case=case['name']):
                self.assertTrue(validator.is_valid(dict(type='hello',version=1,capabilities=case['capabilities'])))
                self.assertTrue(validator.is_valid(dict(type='meetings',meetings=[case['meeting']])))
                self.assertIs(type(case['expect']['automatic']),bool)
                self.assertIs(type(case['expect']['manual']),bool)
                # D52.5: optional app-side recordings, matched to the row by tabId plus
                # callGeneration across every control peer, and the optional resume
                # expectation (absent means false).
                self.assertIs(type(case['expect'].get('resume',False)),bool)
                self.assertLessEqual(set(case['expect']),{'automatic','manual','resume'})
                for recording in case.get('recordings',[]):
                    self.assertLessEqual({'captureId','tabId','callGeneration','othersSource'},set(recording))
                    self.assertLessEqual(set(recording),{'captureId','tabId','callGeneration','othersSource','heardSeen','controlPeer'})
                    self.assertIs(type(recording.get('heardSeen',False)),bool)
                    self.assertIn(recording.get('controlPeer','current'),['current','earlier'])
                    self.assertTrue(validator.is_valid(dict(type='meetings',meetings=[dict(case['meeting'],tabId=recording['tabId'],callGeneration=recording['callGeneration'])])))
                    self.assertEqual(recording['othersSource'],dict(source='system',reason='lost'))
                if case.get('recordings'): self.assertIn('callGeneration',case['meeting'])


    def test_d33_page_state_and_opt_in_are_additive_and_typed(self):
        extension=Draft202012Validator(json.loads((ROOT/'schemas/control-extension.schema.json').read_text()))
        host=Draft202012Validator(json.loads((ROOT/'schemas/control-host.schema.json').read_text()))
        for state in ['present','closed','navigating']:
            row=dict(type='status',recording=True,tabId=2,path='/Recording-test',message='',pageState=state)
            self.assertTrue(extension.is_valid(row))
            self.assertTrue(extension.is_valid(dict(row,sessions=[row])))
        for state in ['missing',None,1]:
            self.assertFalse(extension.is_valid(dict(row,pageState=state)))
        start=dict(type='command',requestId='s',action='start',mode='tracks',tabId=2,independent=True,keepOnNavigation=True)
        self.assertTrue(host.is_valid(start))
        self.assertFalse(host.is_valid(dict(start,independent=False)))
        self.assertFalse(host.is_valid(dict(start,keepOnNavigation='true')))
        self.assertFalse(host.is_valid(dict(start,reason='user')))
        stop=dict(type='command',requestId='s',action='stop',tabId=2,reason='tabClosed')
        self.assertTrue(host.is_valid(stop))
        self.assertFalse(host.is_valid(dict(stop,reason='unknown')))

    def test_host_schema_matches_valid_and_invalid_commands(self):
        validator=Draft202012Validator(json.loads((ROOT/'schemas/control-host.schema.json').read_text()))
        invalid={'invalid-action','invalid-tab-id','missing-mode','whole-tab-needs-browser-click'}
        for case in json.loads((ROOT/'conformance/control/vectors.json').read_text()):
            for command in case.get('commands',[]):
                self.assertEqual(validator.is_valid(command),case['name'] not in invalid,case['name'])
        self.assertTrue(validator.is_valid(dict(type='command',requestId=0,action='stop',tabId=0,mode='ignored-extra')))
        self.assertFalse(validator.is_valid(dict(type='command',requestId=True,action='stop',tabId=2)))

    def test_mutated_control_hosts_are_rejected(self):
        cases = json.loads((ROOT / 'conformance/control/vectors.json').read_text())
        fixture = ROOT / 'conformance/control/fixture-host.py'
        case = next(c for c in cases if c['name'] == 'start-by-tab-id')
        for fault in ['status', 'tab']:
            with self.subTest(fault=fault), patch.dict(os.environ, CASSINI_CONTROL_FAULT=fault), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(runner.run(fixture, case))

    def test_audio_vectors_and_runner_are_unchanged(self):
        import hashlib
        self.assertEqual(len(json.loads((ROOT / 'conformance/vectors.json').read_text())), 88)
        # D52.2 Q7 added host-derived gapFrames/state to the two legacy gap vectors.
        self.assertEqual(hashlib.sha256((ROOT / 'conformance/vectors.json').read_bytes()).hexdigest(), 'd79a75fcf3608ab652f11fc89d5426c9476737bc9f5f22bbb6260b397b0f35e1')
        # The runner gained per-case private Libraries (test Library isolation).
        self.assertEqual(hashlib.sha256((ROOT / 'conformance/run.py').read_bytes()).hexdigest(), 'e1c01b919bcfb465e511058d285171f9ff1095f9193b8ca4684cc40236a26cb1')

if __name__ == '__main__':
    unittest.main()
