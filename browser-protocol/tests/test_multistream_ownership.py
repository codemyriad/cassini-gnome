"""Literal D49/restoration seam fixtures catch doubled or unmasked mix inputs."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ownership', ROOT / 'conformance/multistream/ownership.py')
owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
spec = importlib.util.spec_from_file_location('annotations', ROOT / 'conformance/check-session-events.py')
annotations = importlib.util.module_from_spec(spec); spec.loader.exec_module(annotations)

class OwnershipTests(unittest.TestCase):
    def test_literal_windows_and_single_owner_at_the_seams(self):
        cases = json.loads((ROOT / 'conformance/multistream/ownership.json').read_text())['cases']
        for case in cases:
            with self.subTest(case=case['name']):
                actual = {t['id']: owner.windows(case['events'], t) for t in case['tracks']}
                self.assertEqual(actual, case['expected'])
                for sample in case['ownershipAt']:
                    self.assertEqual([k for k,v in actual.items() if any(a <= sample['atMs'] < b for a,b in v)], sample['ids'])
                    self.assertLessEqual(sum(any(a <= sample['atMs'] < b for a,b in v) for v in actual.values()), 1)
    def test_D49_reason_requires_the_missing_browser_stand_in(self):
        baseline = json.loads((annotations.FIXTURES / 'session.json').read_text())
        baseline['events'] = [dict(type='selection',atMs=0,me=True,others=True,reason='start'),
                              dict(type='othersSource',atMs=0,source='system',reason='missing'),
                              dict(type='othersSource',atMs=1,source='browser',reason='callConnected')]
        annotations.validate(baseline)
        for mutation in ('no-history','disabled','wrong-source','wrong-history'):
            case = copy.deepcopy(baseline)
            if mutation == 'no-history': del case['events'][1]
            if mutation == 'disabled': case['events'][0]['others'] = False
            if mutation == 'wrong-source': case['events'][-1]['source'] = 'system'
            if mutation == 'wrong-history': case['events'][1]['reason'] = 'user'
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, ValidationError)): annotations.validate(case)
