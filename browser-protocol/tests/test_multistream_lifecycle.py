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
    def test_transport_loss_with_present_page_never_arms_page_gone(self):
        # D52.4 Q3: treating a closed audio transport as missing-page evidence
        # (a 2 s missing timer and the 120 s pageGone deadline) must fail.
        cases={c['name']:c for c in json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())['cases']}
        case=cases['transport-lost-with-present-page-stays-on-fallback']
        missing=policy.Policy()
        actual=[missing.step(dict(s,controlAlive=s.get('controlAlive',True) and s.get('transportAlive',True))) for s in case['steps']]
        self.assertNotEqual(actual,[s['expect'] for s in case['steps']])
        sticky=policy.Policy()  # ignoring transportAlive never falls back
        self.assertNotEqual([sticky.step({k:v for k,v in s.items() if k!='transportAlive'}) for s in case['steps']],[s['expect'] for s in case['steps']])
    def test_transport_loss_falls_back_at_once_whatever_the_page_or_count(self):
        # Reviewer mutant: falling back on a closed transport only for a present
        # page with a known positive count must fail the shared vectors.
        vectors=json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())
        def visible_only(s):
            count=s.get('sourceCount')
            known=s.get('rowPresent',True) and s.get('sourceCountKnown',count is not None)
            visible=s.get('pageState','present')=='present' and s.get('controlAlive',True) and known and count>0
            return s if visible else {k:v for k,v in s.items() if k!='transportAlive'}
        failing=set()
        for case in vectors['cases']:
            mutant=policy.Policy(case.get('source','browser'),case.get('sourceReason','start'))
            if [mutant.step(visible_only(s)) for s in case['steps']]!=[s['expect'] for s in case['steps']]: failing.add(case['name'])
        self.assertEqual(failing,{'transport-lost-with-unknown-count-falls-back-at-once',
            'transport-lost-during-zero-count-grace-keeps-the-call-end-deadline','transport-lost-while-navigating-falls-back-at-once'})
