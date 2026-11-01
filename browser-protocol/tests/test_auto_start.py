"""D30.1/D52.5: the hand-written auto-start expectations agree with an independent oracle."""
import importlib.util
import json
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auto_start', ROOT / 'conformance/control/auto_start.py')
oracle = importlib.util.module_from_spec(spec); spec.loader.exec_module(oracle)
CASES = json.loads((ROOT / 'conformance/control/auto-start-vectors.json').read_text())
LOST_CALL = 'lost-recording-same-call-resumes-instead-of-auto-start'


def expected(case):
    return dict(case['expect'], resume=case['expect'].get('resume', False))


def failing(decide):
    return {case['name'] for case in CASES if decide(case) != expected(case)}


class AutoStartOracleTests(unittest.TestCase):
    def test_oracle_reproduces_every_literal_case(self):
        for case in CASES:
            with self.subTest(case=case['name']):
                self.assertEqual(oracle.decide(case), expected(case))

    def test_lost_call_cases_are_present_and_only_resume_needs_a_lost_recording(self):
        names = {case['name'] for case in CASES}
        self.assertLessEqual({LOST_CALL, 'lost-recording-same-call-unheard-neither-starts-nor-resumes',
                              'lost-recording-other-call-generation-may-auto-start',
                              'lost-recording-other-tab-may-auto-start',
                              'lost-recording-same-call-heard-earlier-resumes',
                              'lost-recording-under-an-earlier-control-peer-resumes-on-a-new-hello'}, names)
        for case in CASES:
            if expected(case)['resume']:
                self.assertIsNotNone(oracle.lost_recording(case), case['name'])
                self.assertFalse(case['expect']['automatic'], case['name'])

    def test_mutant_that_auto_starts_the_lost_call_fails(self):
        def ignores_recordings(case):
            return oracle.decide({k: v for k, v in case.items() if k != 'recordings'})
        self.assertIn(LOST_CALL, failing(ignores_recordings))

        def starts_and_resumes(case):
            return dict(oracle.decide(case), automatic=oracle.decide({k: v for k, v in case.items() if k != 'recordings'})['automatic'])
        self.assertIn(LOST_CALL, failing(starts_and_resumes))

    def test_mutant_that_blocks_by_tab_only_or_generation_only_fails(self):
        def tab_only(case):
            recordings = [dict(r, callGeneration=case['meeting'].get('callGeneration')) for r in case.get('recordings', [])]
            return oracle.decide(dict(case, recordings=recordings))
        self.assertIn('lost-recording-other-call-generation-may-auto-start', failing(tab_only))

        def generation_only(case):
            recordings = [dict(r, tabId=case['meeting']['tabId']) for r in case.get('recordings', [])]
            return oracle.decide(dict(case, recordings=recordings))
        self.assertIn('lost-recording-other-tab-may-auto-start', failing(generation_only))

    def test_mutant_that_lets_record_all_start_the_lost_call_fails(self):
        def record_all_ignores_recording(case):
            return dict(oracle.decide(case), manual=case['meeting']['sourceCount'] > 0)
        self.assertIn(LOST_CALL, failing(record_all_ignores_recording))

    def test_mutant_that_matches_by_control_peer_fails(self):
        # Review #32: lost recordings match by (tabId, callGeneration) across all
        # control peers, past and present, never by the peer or connection.
        def same_peer_only(case):
            recordings = [r for r in case.get('recordings', []) if r.get('controlPeer', 'current') == 'current']
            return oracle.decide(dict(case, recordings=recordings))
        self.assertIn('lost-recording-under-an-earlier-control-peer-resumes-on-a-new-hello', failing(same_peer_only))

    def test_heard_latch_is_explicit_and_mutants_that_ignore_it_fail(self):
        unheard = next(c for c in CASES if c['name'] == 'lost-recording-same-call-unheard-neither-starts-nor-resumes')
        self.assertIs(unheard['recordings'][0]['heardSeen'], False)
        def seeded_latched(case):  # seeds every recording as already heard
            recordings = [dict(r, heardSeen=True) for r in case.get('recordings', [])]
            return oracle.decide(dict(case, recordings=recordings))
        self.assertIn(unheard['name'], failing(seeded_latched))
        def current_row_only(case):  # forgets the per-generation latch
            recordings = [dict(r, heardSeen=False) for r in case.get('recordings', [])]
            return oracle.decide(dict(case, recordings=recordings))
        self.assertIn('lost-recording-same-call-heard-earlier-resumes', failing(current_row_only))


if __name__ == '__main__':
    unittest.main()
