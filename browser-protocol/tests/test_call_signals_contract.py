"""D74/D80 literal/control-schema checks; not a producer or app audio engine."""
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

    def test_native_start_correlates_retired_automatic_and_later_manual_same_call(self):
        for case in VECTORS['nativeStartCorrelation']['cases']:
            with self.subTest(name=case['name']):
                for command in case['commands']:
                    schema('control-host').validate(command)
                for packet in case['expectedNativeStarts']:
                    schema('request').validate(packet)
                starts = [command for command in case['commands'] if command['action'] == 'start']
                native = case['expectedNativeStarts']
                self.assertEqual([packet['requestId'] for packet in native],
                                 [command['requestId'] for command in starts])
                self.assertEqual(len({packet['callGeneration'] for packet in native}), 1)
                self.assertNotEqual(case['retiredAutomaticRequestId'], case['pendingManualRequestId'])
                arrival = [native[index]['requestId'] for index in case['nativeArrivalOrder']]
                self.assertEqual(arrival, [case['expect']['acceptManualRequestId'],
                                           case['expect']['rejectRetiredAutomaticRequestId']])
                self.assertEqual(case['expect']['lateAutomaticInputs'], 0)
                self.assertEqual(case['expect']['lateAutomaticOutputs'], 0)

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

    def test_passive_media_literals_never_start_and_preserve_real_rtp(self):
        cases = VECTORS['passiveMediaNegatives']
        self.assertEqual(len(cases), 6)
        for case in cases:
            schema('control-extension').validate(case['signal'])
            self.assertTrue(case['fixtureAudioPlaying'])
            self.assertTrue(case['autoRecordOptIn'])  # opt-in alone is insufficient
            self.assertEqual(case['signal']['tabs'][0]['callEvidence']['state'], 'unknown')
            for field in ['signalAutoCandidate', 'appSystemStart', 'browserStart']:
                self.assertFalse(case['expect'][field])
            rtc = case['name'] == 'passive-rtc-viewer'
            self.assertEqual(case['expect']['legacySourceCount'], 1 if rtc else 0)
            self.assertEqual(case['expect']['legacyHeard'], rtc)
            if rtc:
                self.assertFalse(case['fixtureGetUserMedia'])
                self.assertFalse(case['fixtureLocalCapturedSender'])
                self.assertEqual(case['fixtureHookCallGeneration'], '0')
                self.assertEqual(case['rtp']['callGeneration'], 'call-A')
                self.assertFalse(case['expect']['nativeFallback'])
                self.assertFalse(case['expect']['inputOpened'])

    def test_verified_rtp_admission_joins_explicit_namespaces_not_raw_ids(self):
        cases = [case for case in VECTORS['routing'] if 'configuredCapabilities' in case
                 and case['signal']['tabs'][0]['remoteAudio'] == 'separated']
        cases += [case for case in VECTORS['passiveMediaNegatives'] if case['name'] == 'passive-rtc-viewer']
        for case in cases:
            with self.subTest(name=case['name']):
                row = case['signal']['tabs'][0]
                schema('control-extension').validate(case['signal'])
                schema('control-extension').validate(dict(type='meetings', meetings=[dict(
                    case['rtp'], title='Controlled media', url='https://conference.invalid/session')]))
                binding = self.binding_matches(case)
                self.assertEqual(binding, case['expect']['bindingMatches'])
                ready = (case['rtp']['sourceCountKnown'] and case['rtp']['sourceCount'] > 0
                         and case['rtp']['heard'] and case['rtp']['pageState'] == 'present')
                negotiated = set(case['configuredCapabilities']) == {
                    'call-signals-v1', 'verified-auto-admission-v1'}
                fresh = 0 <= case['consumerEvidenceAgeMs'] < VECTORS['freshness']['consumerActiveExpiryMs']
                automatic = (case['autoRecordOptIn'] and negotiated and binding and ready and fresh
                             and row['callEvidence']['state'] == 'active' and row['remoteAudio'] == 'separated')
                self.assertEqual(automatic, case['expect']['browserStart'])
        positive = next(c for c in cases if c['name'] == 'separated-existing-rtp-ready')
        self.assertNotEqual(positive['rtp']['documentGeneration'],
                            positive['signal']['tabs'][0]['documentGeneration'])
        self.assertEqual(positive['expect']['policy'], 'verified-call-and-RTP')

    def test_mixed_system_admission_requires_verified_ack_alias_binding_and_freshness_without_rtp(self):
        cases = [case for case in VECTORS['routing'] if 'configuredCapabilities' in case
                 and case['signal']['tabs'][0]['remoteAudio'] == 'mixed']
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(name=case['name']):
                schema('control-extension').validate(case['signal'])
                row = case['signal']['tabs'][0]
                pending = case['pendingBinding']
                identities = [row.get('documentGeneration'), row.get('sourceDocumentGeneration'),
                              row['callEvidence'].get('callGeneration')]
                binding = (all(isinstance(value, str) and 0 < len(value.encode()) <= 200 for value in identities)
                           and case['connectionBrowserInstance'] == case['signal']['browserInstance']
                           and pending['browserInstance'] == case['signal']['browserInstance']
                           and pending['tabId'] == row['tabId']
                           and pending['browserDocumentId'] == row['documentGeneration']
                           and pending['sourceDocumentGeneration'] == row['sourceDocumentGeneration']
                           and pending['callGeneration'] == row['callEvidence']['callGeneration'])
                self.assertEqual(binding, case['expect']['bindingMatches'])
                negotiated = set(case['configuredCapabilities']) == {'call-signals-v1', 'verified-auto-admission-v1'}
                fresh = 0 <= case['consumerEvidenceAgeMs'] < VECTORS['freshness']['consumerActiveExpiryMs']
                system = case['autoRecordOptIn'] and negotiated and fresh and binding and row['callEvidence']['state'] == 'active'
                self.assertEqual(system, case['expect']['appSystemStart'])
                self.assertFalse(case['expect']['browserStart'])
                self.assertEqual(case['legacyMeetings'], [], 'signal-only system route cannot require a legacy/RTP row')
        positive = next(case for case in cases if case['name'] == 'mixed-positive-opt-in')
        self.assertEqual(positive['rtp']['sourceCount'], 0)
        self.assertFalse(positive['rtp']['heard'])

    def test_numeric_freshness_expires_with_healthy_heartbeat_and_rejects_cached_renewal_on_both_routes(self):
        policy = VECTORS['freshness']
        self.assertEqual([policy[key] for key in ['nativeProgressMaxAgeMs', 'activeRefreshMaxIntervalMs',
                                                  'producerWithdrawMaxDelayMs', 'consumerActiveExpiryMs']],
                         [2500, 2000, 5000, 5000])
        self.assertEqual(set(policy['routes']), {'separated', 'mixed'})
        self.assertEqual(VECTORS['appPolicy']['evidenceLossGraceMs'], 120000)
        prerequisites = VECTORS['appPolicy']['admissionPrerequisites']
        self.assertEqual(set(prerequisites['configuredCapabilities']), {'call-signals-v1', 'verified-auto-admission-v1'})
        self.assertTrue(prerequisites['nonNullSourceAlias'])
        self.assertEqual(prerequisites['consumerActiveExpiryMs'], 5000)
        self.assertEqual(set(prerequisites['routes']), {'separated-browser', 'mixed-system', 'automatic-native-fallback'})
        self.assertIn('not callEnded', policy['existingOwnerOnProofExpiry'])
        pair = {'call-signals-v1', 'verified-auto-admission-v1'}
        for route in policy['routes']:
            for case in policy['cases']:
                with self.subTest(route=route, case=case['name']):
                    configured = set(case['configuredCapabilities'])
                    last_receipt = last_progress = None
                    previous_counts = None
                    last_sequence = -1
                    for step in case['steps']:
                        at, event = step['atMs'], step['event']
                        renewed = False
                        if event in ['active', 'attempt-active-refresh']:
                            counts = (step['receivedCounter'], step['sentCounter'])
                            progressed = previous_counts is None or all(a > b for a, b in zip(counts, previous_counts))
                            ages = [at - step[key] for key in ['receivedProgressAtMs', 'sentProgressAtMs']]
                            producer_valid = configured == pair and progressed and all(0 <= age <= policy['nativeProgressMaxAgeMs'] for age in ages)
                            self.assertEqual(producer_valid, step['expect']['producerActiveAllowed'])
                            if event == 'active':
                                self.assertTrue(producer_valid)
                                self.assertGreater(step['sequence'], last_sequence)
                                last_sequence = step['sequence']
                                last_receipt = at
                                last_progress = min(step['receivedProgressAtMs'], step['sentProgressAtMs'])
                                previous_counts = counts
                                renewed = True
                            else:
                                # The producer must not emit this attempted cached
                                # refresh. The app does not see hidden RTP counters.
                                self.assertFalse(producer_valid)
                        elif event == 'configured':
                            configured = set(step['capabilities'])
                            last_receipt = None
                        elif event == 'replayed-active':
                            self.assertLessEqual(step['sequence'], last_sequence)
                        fresh = (configured == pair and last_receipt is not None
                                 and 0 <= at - last_receipt < policy['consumerActiveExpiryMs'])
                        must_withdraw = last_progress is not None and at - last_progress >= policy['producerWithdrawMaxDelayMs']
                        self.assertEqual(renewed, step['expect']['leaseRenewed'])
                        self.assertEqual(fresh, step['expect']['consumerFresh'])
                        self.assertEqual(fresh, step['expect']['newAdmissionEligible'])
                        self.assertEqual(must_withdraw, step['expect']['producerMustWithdraw'])

    def test_d805_document_frame_port_cohort_accepts_split_media_but_never_crosses_identity(self):
        meaning = VECTORS['qualificationMeaning']
        self.assertEqual(meaning['decision'], 'D80.5')
        cases = {case['name']: case for case in meaning['cases']}
        for case in cases.values():
            bound = all(case['receiverBinding'][field] == case['senderBinding'][field]
                        for field in ['browserDocumentId', 'frameId', 'portIncarnation'])
            fresh = all(0 <= case[field] <= VECTORS['freshness']['nativeProgressMaxAgeMs']
                        for field in ['receivedProgressAgeMs', 'sentProgressAgeMs'])
            active = bound and fresh and all(case[field] for field in ['connectedSession', 'liveReceivedProgress',
                         'pageCapturedRootOrClone', 'capturedTrackActuallySent'])
            self.assertEqual('active' if active else 'unknown', case['expectState'])
        for name in ['echo-or-microphone-test', 'unanswered-early-media', 'unrelated-sending-pc',
                     'split-publisher-subscriber', 'unrelated-mic-same-backend']:
            self.assertEqual(cases[name]['expectState'], 'active')
        self.assertEqual(cases['passive-webrtc-viewer']['expectState'], 'unknown')
        for name in ['cross-document-split', 'cross-frame-split', 'retired-port-split',
                     'stale-received-half', 'stale-sent-half', 'mic-preview', 'synthetic-sender']:
            self.assertEqual(cases[name]['expectState'], 'unknown')
        for route in VECTORS['routing']:
            if 'qualificationCase' in route:
                state = cases[route['qualificationCase']]['expectState']
                self.assertEqual(route['signal']['tabs'][0]['callEvidence']['state'], state)
                self.assertEqual(route['expect']['browserStart'], state == 'active')

    @staticmethod
    def binding_matches(case):
        row, rtp = case['signal']['tabs'][0], case['rtp']
        identities = [row.get('documentGeneration'), row.get('sourceDocumentGeneration'),
                      rtp.get('browserDocumentId'), rtp.get('documentGeneration'),
                      row['callEvidence'].get('callGeneration'), rtp.get('callGeneration')]
        return (all(isinstance(value, str) and 0 < len(value.encode()) <= 200 for value in identities)
                and case['connectionBrowserInstance'] == case['signal']['browserInstance']
                and rtp['tabId'] == row['tabId']
                and rtp['browserDocumentId'] == row['documentGeneration']
                and rtp['documentGeneration'] == row['sourceDocumentGeneration']
                and rtp['callGeneration'] == row['callEvidence']['callGeneration'])

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
        self.assertEqual(set(case['configuredCapabilities']), {'call-signals-v1', 'verified-auto-admission-v1'})
        self.assertEqual([s['signal']['tabs'][0]['callEvidence']['state'] for s in case['steps']],
                         ['active', 'inactive', 'unknown'])
        for step in case['steps']:
            schema('control-extension').validate(step['signal'])
            self.assertNotEqual(step['signal']['tabs'][0]['documentGeneration'],
                                step['signal']['tabs'][0]['sourceDocumentGeneration'])
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
