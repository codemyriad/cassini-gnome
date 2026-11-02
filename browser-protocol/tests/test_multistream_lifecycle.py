"""The new long-grace corpus remains distinct from legacy D33 envelopes."""
import importlib.util
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('policy',ROOT/'conformance/multistream/lifecycle.py')
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
def make(case,cls=None):
    return (cls or policy.Policy)(case.get('source','browser'),case.get('sourceReason','start'),case.get('resumeCapture',False),case.get('callGeneration'))
class LifecycleTests(unittest.TestCase):
    def test_literal_cases(self):
        vectors=json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())
        self.assertEqual(vectors['constants'],dict(OTHERS_GONE_GRACE=30000,TAB_CLOSED_GRACE=2000,BROWSER_RETURN_GRACE=120000))
        for case in vectors['cases']:
            with self.subTest(case=case['name']):
                actual=make(case)
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
            mutant=make(case)
            if [mutant.step(visible_only(s)) for s in case['steps']]!=[s['expect'] for s in case['steps']]: failing.add(case['name'])
        self.assertEqual(failing,{'transport-lost-with-unknown-count-falls-back-at-once',
            'transport-lost-during-zero-count-grace-keeps-the-call-end-deadline','transport-lost-while-navigating-falls-back-at-once',
            'D52.5-ask-waits-for-control-present-page-row-known-positive-count-and-same-call'})

D52_5={'D52.5-transport-lost-asks-resume-once-then-restores','D52.5-refused-resume-is-not-retried-on-a-timer-only-on-a-new-hello',
    'D52.5-frozen-companion-asks-once-per-new-control-connection','D52.5-ask-waits-for-control-present-page-row-known-positive-count-and-same-call',
    'D52.5-ask-needs-heard-in-the-same-call-generation','D52.5-a-new-loss-after-restore-asks-again',
    'D52.5-other-call-heard-does-not-arm-grace-or-restore'}
def mutated(old,new):
    source=(ROOT/'conformance/multistream/lifecycle.py').read_text()
    assert source.count(old)==1,old
    namespace={};exec(compile(source.replace(old,new),'lifecycle-mutant','exec'),namespace)
    return namespace['Policy']
def failing_cases(cls,rewrite=lambda s:s):
    failing=set()
    for case in json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())['cases']:
        mutant=make(case,cls)
        if [mutant.step(rewrite(s)) for s in case['steps']]!=[s['expect'] for s in case['steps']]: failing.add(case['name'])
    return failing
class ResumeOnceTests(unittest.TestCase):
    """D52.5: ask to resume a system/lost recording at most once per loss and control connection."""
    def test_vectors_cover_the_rule_and_refusals_follow_an_ask(self):
        cases={c['name']:c for c in json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())['cases']}
        self.assertLessEqual(D52_5,set(cases))
        for name,case in cases.items():
            self.assertEqual(name in D52_5,case.get('resumeCapture',False) is True,name)
            asked=set()
            for step in case['steps']:
                if 'askResume' in step['expect']: asked.add(step.get('controlConnection',0))
                if step.get('resumeRefused'): self.assertIn(step.get('controlConnection',0),asked,(name,step))
        # Without resume-capture-v1 the same rows never ask (the preserved D52.4 cases).
        for name in D52_5:
            plain=dict(cases[name]);plain.pop('resumeCapture')
            actual=make(plain)
            self.assertNotIn('askResume',[a for s in plain['steps'] for a in actual.step(s)])
    def test_timer_or_refusal_retry_mutants_fail(self):
        def timer(interval):
            class Timer(policy.Policy):
                asked_at=None
                def step(self,row):
                    if self.asked_on is not None and row['atMs']-self.asked_at>=interval: self.asked_on=None
                    actions=super().step(row)
                    if 'askResume' in actions: self.asked_at=row['atMs']
                    return actions
            return Timer
        for interval in [1000,10000,30000,60000,120000]:
            with self.subTest(interval=interval):
                self.assertIn('D52.5-refused-resume-is-not-retried-on-a-timer-only-on-a-new-hello',failing_cases(timer(interval)))
        class AfterRefusal(policy.Policy):
            def step(self,row):
                if row.get('resumeRefused'): self.asked_on=None
                return super().step(row)
        class AfterRefusalNextTick(policy.Policy):
            refused=False
            def step(self,row):
                if self.refused: self.asked_on=None
                self.refused=row.get('resumeRefused') is True
                return super().step(row)
        self.assertTrue(failing_cases(AfterRefusal)&D52_5)
        self.assertTrue(failing_cases(AfterRefusalNextTick)&D52_5)
    def test_never_reasking_on_a_new_hello_or_ignoring_the_generation_fails(self):
        self.assertTrue(failing_cases(None,lambda s:{k:v for k,v in s.items() if k!='controlConnection'})&D52_5)
        # Review #32: heard scoped to the recording's generation, but the ask
        # ignores the current row's callGeneration.
        ask_any_row=mutated("and generation == self.generation and self.heard","and self.heard")
        self.assertIn('D52.5-ask-waits-for-control-present-page-row-known-positive-count-and-same-call',failing_cases(ask_any_row))
        # Review #32 (agy): heard latched from any generation pollutes restore and the grace timer.
        unscoped=mutated("row.get('heard') is True and self.generation in (None, generation)","row.get('heard') is True")
        self.assertIn('D52.5-other-call-heard-does-not-arm-grace-or-restore',failing_cases(unscoped))
        # A navigating page with a positive count is not a present page.
        navigating_is_present=lambda s:dict(s,pageState='present') if s.get('pageState')=='navigating' and s.get('sourceCount',0)>0 else s
        self.assertIn('D52.5-ask-waits-for-control-present-page-row-known-positive-count-and-same-call',failing_cases(None,navigating_is_present))
        class OncePerRecording(policy.Policy):  # never asks twice on one connection, even after a new loss
            def step(self,row):
                self.ever=getattr(self,'ever',set());connection=row.get('controlConnection',0)
                actions=super().step(row)
                if 'askResume' in actions and connection in self.ever: actions.remove('askResume')
                if 'askResume' in actions: self.ever.add(connection)
                return actions
        self.assertIn('D52.5-a-new-loss-after-restore-asks-again',failing_cases(OncePerRecording))
class StaleAfterTests(unittest.TestCase):
    def test_transport_stale_cases_close_at_pcm_stale_after_45_seconds(self):
        # D52.5 amendment: PCM_STALE_AFTER is 45 s (40 s = 30 s retry + 10 s ACK left
        # no margin under >=). Cases whose last audio frame is at 0 close at 45000.
        self.assertIn('`PCM_STALE_AFTER = 45 s`',(ROOT/'SPEC.md').read_text())
        self.assertNotIn('PCM_STALE_AFTER = 40',(ROOT/'SPEC.md').read_text())
        cases={c['name']:c for c in json.loads((ROOT/'conformance/multistream/lifecycle.json').read_text())['cases']}
        for name in ['frozen-companion-control-lost-then-transport-stale-then-returns','frozen-companion-never-returns',
                'transport-lost-with-present-page-stays-on-fallback','transport-lost-then-new-transport-restores',
                'transport-lost-with-unknown-count-falls-back-at-once','D52.5-transport-lost-asks-resume-once-then-restores',
                'D52.5-refused-resume-is-not-retried-on-a-timer-only-on-a-new-hello','D52.5-frozen-companion-asks-once-per-new-control-connection',
                'D52.5-ask-needs-heard-in-the-same-call-generation','D52.5-a-new-loss-after-restore-asks-again']:
            with self.subTest(case=name):
                self.assertEqual(next(s['atMs'] for s in cases[name]['steps'] if s.get('transportAlive') is False),45000)
