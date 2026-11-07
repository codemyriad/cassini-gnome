"""D74 literal/control-schema checks; not a site detector or app audio engine."""
import copy
import json
import unittest
from pathlib import Path
from test_schemas import schema
ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / 'conformance/control/call-signals-vectors.json').read_text())

class CallSignalContractTests(unittest.TestCase):
    def test_literal_message_shapes(self):
        for group, accepted in [('valid', True), ('invalid', False)]:
            for case in VECTORS[group]:
                with self.subTest(group=group, name=case['name']):
                    validator = schema(case['schema'])
                    self.assertEqual(validator.is_valid(case['message']), accepted,
                                     list(validator.iter_errors(case['message'])))

    def test_unknown_call_and_remote_design_are_independent(self):
        message = copy.deepcopy(next(c['message'] for c in VECTORS['valid']
                                     if c['name'] == 'mixed-design-unknown-call'))
        validator = schema('control-extension')
        validator.validate(message)
        for classification in ['mixed', 'separated', 'unknown']:
            message['tabs'][0]['remoteAudio'] = classification
            validator.validate(message)
            self.assertEqual(message['tabs'][0]['callEvidence']['state'], 'unknown')
        message['tabs'][0]['callEvidence']['state'] = 'inactive'
        self.assertFalse(validator.is_valid(message), 'unknown evidence cannot become affirmative inactive')

    def test_passive_media_literals_never_start_and_do_not_fabricate_rtp(self):
        cases = VECTORS['passiveMediaNegatives']
        self.assertEqual(len(cases), 5)
        for case in cases:
            schema('control-extension').validate(case['signal'])
            self.assertTrue(case['fixtureAudioPlaying'])
            self.assertTrue(case['autoRecordOptIn'])  # opt-in alone is insufficient
            self.assertEqual(case['signal']['tabs'][0]['callEvidence']['state'], 'unknown')
            for field in ['signalAutoCandidate', 'appSystemStart', 'browserStart', 'legacyHeard']:
                self.assertFalse(case['expect'][field])
            self.assertEqual(case['expect']['legacySourceCount'], 0)

    def test_routing_literals_distinguish_opt_in_system_and_existing_rtp(self):
        cases = {case['name']: case for case in VECTORS['routing']}
        for case in cases.values():
            schema('control-extension').validate(case['signal'])
        self.assertTrue(cases['mixed-positive-opt-in']['expect']['appSystemStart'])
        self.assertFalse(cases['mixed-positive-opt-out']['expect']['appSystemStart'])
        self.assertFalse(cases['mixed-with-one-rtp-still-not-browser']['expect']['browserStart'])
        self.assertEqual(cases['mixed-with-one-rtp-still-not-browser']['expect']['browserCommandRefused'], 'unsupported')
        self.assertTrue(cases['separated-existing-rtp-ready']['expect']['browserStart'])
        self.assertFalse(cases['separated-signal-without-rtp-is-insufficient']['expect']['browserStart'])
        self.assertFalse(cases['active-call-unknown-design-no-guess']['expect']['appSystemStart'])

    def test_positive_end_stops_associated_auto_call_while_media_continues(self):
        case = VECTORS['lifecycle']
        self.assertEqual([s['signal']['tabs'][0]['callEvidence']['state'] for s in case['steps']],
                         ['active', 'inactive', 'unknown'])
        for step in case['steps']:
            schema('control-extension').validate(step['signal'])
            self.assertTrue(step['fixtureAudioPlaying'])
        self.assertEqual([s['expect']['appSystemStart'] for s in case['steps']], [True, False, False])
        self.assertEqual([s['expect']['appStop'] for s in case['steps']], [False, True, False])
        ended = case['steps'][1]['signal']['tabs'][0]['callEvidence']
        self.assertEqual((ended['reason'], ended['callGeneration']), ('callEnded', 'call-A'))
        self.assertEqual(case['steps'][1]['expect']['stopAssociation'],
                         dict(browserInstance='profile-A', tabId=2, documentGeneration='document-A', callGeneration='call-A'))

    def test_known_classification_and_inactive_state_need_adapter(self):
        message = copy.deepcopy(next(c['message'] for c in VECTORS['valid']
                                     if c['name'] == 'mixed-design-unknown-call'))
        message['tabs'][0]['callEvidence']['adapter'] = None
        self.assertFalse(schema('control-extension').is_valid(message))
        message['tabs'][0]['remoteAudio'] = 'unknown'
        schema('control-extension').validate(message)
        message['tabs'][0]['callEvidence'].update(state='inactive', reason='nonCall')
        self.assertFalse(schema('control-extension').is_valid(message))

class AppPolicyLiteralTests(unittest.TestCase):
    """D74.1 app contract literals; actual app policy E1 tests remain required."""
    def test_replacement_connection_keeps_stable_owner_and_ignores_late_ends(self):
        policy = VECTORS['appPolicy']
        self.assertEqual(policy['identityFields'], ['browserInstance', 'tabId', 'documentGeneration', 'callGeneration'])
        case = policy['ownerReconciliation']
        self.assertNotEqual(case['originalConnection'], case['replacementConnection'])
        self.assertEqual(case['originalAssociation'], case['replacementAssociation'])
        self.assertEqual(set(case['originalAssociation']), set(policy['identityFields']))
        self.assertTrue(case['expect']['reconcileExisting'])
        self.assertFalse(case['expect']['newSystemStart'])
        self.assertTrue(case['expect']['clearEvidenceLossGrace'])
        for late in policy['lateEnds']:
            self.assertTrue(late['expect']['ignore'])
            self.assertFalse(late['expect']['systemStop'])
        self.assertNotEqual(policy['lateEnds'][0]['endConnection'], policy['lateEnds'][0]['authoritativeConnection'])
        self.assertNotEqual(policy['lateEnds'][1]['endAssociation']['callGeneration'],
                            policy['lateEnds'][1]['currentAssociation']['callGeneration'])

    def test_app_loss_grace_is_120_seconds_and_audio_never_resets_it(self):
        policy = VECTORS['appPolicy']
        self.assertEqual(policy['evidenceLossGraceMs'], 120000)
        steps = policy['lossDeadline']['steps']
        self.assertEqual(steps[0]['expect']['deadlineMs'], steps[0]['atMs'] + 120000)
        self.assertEqual({step['expect']['deadlineMs'] for step in steps}, {121000})
        self.assertEqual([step['expect']['stop'] for step in steps], [False, False, False, True])
        for step in steps:
            self.assertTrue(step['systemAudio'])
            self.assertTrue(step['microphoneAudio'])
        returned = policy['sameCallReturn']['steps'][1]
        self.assertTrue(returned['expect']['clearDeadline'])
        self.assertFalse(returned['expect']['newSystemStart'])
        ended = policy['immediateEnd']
        self.assertTrue(ended['fixtureMediaContinues'])
        self.assertLess(ended['atMs'], ended['priorDeadlineMs'])
        self.assertTrue(ended['expect']['stopNow'])
        self.assertFalse(ended['expect']['waitForSilence'])

    def test_route_is_fixed_and_overlapping_calls_share_one_owner(self):
        policy = VECTORS['appPolicy']
        route = policy['fixedRoute']
        self.assertEqual([step['remoteAudio'] for step in route['steps']], ['mixed', 'separated', 'unknown'])
        self.assertEqual(route['expect']['routeRemains'], 'system')
        for field in ['browserStart', 'browserResume', 'newSystemStart', 'newRecording']:
            self.assertFalse(route['expect'][field])
        shared = policy['sharedOwner']['steps']
        self.assertEqual([step['expect']['associatedCalls'] for step in shared], [1, 2, 1, 0])
        self.assertEqual([step['expect']['systemStarts'] for step in shared], [1, 1, 1, 1])
        self.assertEqual([step['expect']['stop'] for step in shared], [False, False, False, True])
        self.assertNotEqual(shared[0]['association']['tabId'], shared[1]['association']['tabId'])
        grace = policy['sharedOwnerGrace']['steps']
        self.assertEqual([step['expect']['associatedCalls'] for step in grace], [2, 1, 0])
        self.assertEqual([step['expect']['stop'] for step in grace], [False, False, True])
        self.assertFalse(policy['manualStop']['expect']['sameCallAutoRestart'])
        self.assertFalse(policy['manualStop']['expect']['newGenerationSuppressed'])

    def test_details_uses_stored_open_routing_provenance(self):
        route = VECTORS['appPolicy']['detailsProvenance']
        self.assertEqual(route['before'], route['afterCacheRemovalAndExportImport'])
        self.assertEqual(route['before']['recordingDetails']['othersRoute'],
                         dict(source='system', reason='mixedSiteSignal', site='synthetic-site-fixture'))
        self.assertTrue(route['expect']['useStoredRoute'])
        self.assertFalse(route['expect']['inferFromCurrentHostname'])
        self.assertFalse(route['expect']['browserLossAlert'])
        self.assertEqual(json.loads(json.dumps(route['futureOpenStrings'])), route['futureOpenStrings'])

if __name__ == '__main__':
    unittest.main()
