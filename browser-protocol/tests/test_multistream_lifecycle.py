"""The new long-grace corpus remains distinct from legacy D33 envelopes."""
import importlib.util
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('policy',ROOT/'conformance/multistream/lifecycle.py')
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
class LifecycleTests(unittest.TestCase):
    def test_literal_cases(self):
        vectors=json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())
        self.assertEqual(vectors['constants'],dict(OTHERS_GONE_GRACE=30000,TAB_CLOSED_GRACE=2000,BROWSER_RETURN_GRACE=120000))
        for case in vectors['cases']:
            with self.subTest(case=case['name']):
                actual=policy.Policy(case.get('source','browser'),case.get('sourceReason','start'))
                previous=-1
                for step in case['steps']:
                    self.assertGreaterEqual(step['atMs'],previous);previous=step['atMs']
                    self.assertEqual(actual.step(step),step['expect'],step)
    def test_rejoin_case_rejects_the_legacy_two_second_call_end_rule(self):
        spec=importlib.util.spec_from_file_location('legacy',ROOT/'conformance/check-call-lifecycle.py')
        legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
        case=json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())['cases'][0]
        old=legacy.Policy()
        self.assertTrue(any(old.step(dict(pageState='present',**s))!=s['expect'] for s in case['steps']))
