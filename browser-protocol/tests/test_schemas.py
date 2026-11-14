import json
import pathlib
import re
import unittest
from jsonschema import Draft202012Validator, FormatChecker
ROOT = pathlib.Path(__file__).resolve().parent.parent
CLOCK_FACTS = ('clockMethod', 'clockUncertaintyMs', 'clockClampMs')

def schema(name):
    value = json.loads((ROOT / 'schemas' / (name + '.schema.json')).read_text())
    Draft202012Validator.check_schema(value)
    return Draft202012Validator(value, format_checker=FormatChecker())

class SchemaTests(unittest.TestCase):
    def test_examples_fixtures_and_accepted_requests(self):
        request, response, session = schema('request'), schema('response'), schema('session')
        for name in ['start','track','audio','stop']:
            request.validate(json.loads((ROOT / 'conformance/examples' / (name+'.json')).read_text()))
        for value in json.loads((ROOT / 'conformance/examples/responses.json').read_text()): response.validate(value)
        for path in [ROOT/'conformance/examples/session.json',ROOT/'conformance/native-session/session.json',ROOT/'conformance/native-session/session-gnome.json',ROOT/'conformance/gnome-fractional.session.json',ROOT/'conformance/native-session-gap/session.json',ROOT/'conformance/native-session-gap/session-gnome.json']:
            session.validate(json.loads(path.read_text()))
        partial = dict(response.schema); partial.pop('allOf',None)
        matcher = Draft202012Validator(partial)
        for case in json.loads((ROOT/'conformance/vectors.json').read_text()):
            for raw, expect in zip(case['requests'],case['expect']):
                matcher.validate(expect)
                if expect['type'] not in ['started','ack','stopped']: continue
                value = json.loads(raw)
                # D61: an acknowledged track may carry invalid clock facts only where the
                # case expects the host to omit them; everything else stays schema-valid.
                rest = {key: field for key, field in value.items() if key not in CLOCK_FACTS}
                for key in CLOCK_FACTS:
                    if key in value and not request.is_valid(dict(rest, **{key: value[key]})):
                        self.assertIsNone(case['tracks'][value['id']].get(key), (case['name'], value['id'], key))
                    elif key in value: rest[key] = value[key]
                request.validate(rest)

    def test_browser_label_provenance_and_saved_dom(self):
        request, session = schema('request'), schema('session')
        track = dict(type='track', id='named', kind='remote', sampleRate=48000, channels=1)
        source = dict(sourceTrackId='named', kind='remote', label='Hypatia')
        for value in ['browser', 'dom', None]:
            request.validate(dict(track, labelSource=value))
            request.validate(dict(type='sources', discoveredSources=[dict(source, labelSource=value)]))
        for value in ['user', 'device', 'recovered', '', 1, False, {}, []]:
            self.assertFalse(request.is_valid(dict(track, labelSource=value)), value)
            self.assertFalse(request.is_valid(dict(type='sources', discoveredSources=[dict(source, labelSource=value)])), value)
        saved = json.loads((ROOT/'conformance/examples/session.json').read_text())
        saved['tracks'][0]['labelSource'] = 'dom'
        session.validate(saved)

    def test_incomplete_responses_and_overlong_metadata_fail(self):
        response, session = schema('response'), schema('session')
        for kind in ['started','stopped','error']: self.assertFalse(response.is_valid(dict(type=kind)))
        fixture=json.loads((ROOT/'conformance/examples/session.json').read_text())
        for key,value in [('title','x'*501),('state','banana'),('source','banana')]:
            self.assertFalse(session.is_valid(dict(fixture,**{key:value})))
        for key,value in [('label','x'*201),('endOffsetMs',86460001),('id','x'*201)]:
            copy=dict(fixture,tracks=[dict(fixture['tracks'][0],**{key:value})]);self.assertFalse(session.is_valid(copy))
        self.assertFalse(session.is_valid(dict(fixture,problem='failed but reported complete')))
        self.assertFalse(session.is_valid(dict(fixture,state='interrupted')))

    def test_multistream_status_bounds_and_call_connected_source(self):
        control = schema('control-extension')
        value = dict(type='status', recording=True, tabId=2, path='/recording', message='Recording', captureId='x'*200, failedSources=['source'])
        control.validate(value)
        control.validate(dict(type='result',requestId='status',ok=True,status=value))
        self.assertFalse(control.is_valid(dict(value,captureId='x'*201)))
        self.assertFalse(control.is_valid(dict(type='result',requestId='status',ok=True,status=dict(value,captureId='x'*201))))
        session = schema('session')
        meeting = json.loads((ROOT/'conformance/examples/session.json').read_text())
        for source in ('browser','system','none'):
            meeting['events'] = [dict(type='othersSource',source=source,reason='callConnected',atMs=0)]
            self.assertEqual(session.is_valid(meeting), source == 'browser')

    def test_retired_span_marker_cannot_mean_a_finalized_capture(self):
        response = schema('response')
        source = dict(type='error', code='state', scope='source', id='B', retired=True, message='B ended')
        response.validate(source)
        self.assertFalse(response.is_valid(dict(source, scope='command')))
        self.assertFalse(response.is_valid(dict(source, code='io')))
        response.validate(dict(type='error',code='state',scope='command',message='Capture saved'))

    def test_clock_facts_and_track_document_generation(self):
        # D61 (extension 0.2.12): record-only clock facts on the wire and in session.json;
        # documentGeneration is a wire-only liveness hint.
        request, session = schema('request'), schema('session')
        track = dict(type='track', id='x', kind='remote', sampleRate=48000, channels=1)
        clock = dict(clockMethod='roundTrip', clockUncertaintyMs=9, clockClampMs=3)
        for value in [track, dict(track, **clock), dict(track, clockMethod='outputTimestamp', clockUncertaintyMs=0),
                      dict(track, documentGeneration='x' * 200), dict(track, clockMethod=None, clockUncertaintyMs=None, clockClampMs=None, documentGeneration=None)]:
            request.validate(value)
        for key, value in [('clockMethod', 'unknown'), ('clockMethod', 'sampleClock'), ('clockUncertaintyMs', -1), ('clockUncertaintyMs', 1.5),
                           ('clockUncertaintyMs', 86400001), ('clockClampMs', 0), ('clockClampMs', 86400001), ('documentGeneration', ''), ('documentGeneration', 'x' * 201)]:
            with self.subTest(key=key, value=value):
                self.assertFalse(request.is_valid(dict(track, **{key: value})))
        fixture = json.loads((ROOT / 'conformance/examples/session.json').read_text())
        session.validate(dict(fixture, tracks=[dict(fixture['tracks'][0], **clock)]))
        for key, value in [('clockMethod', 'unknown'), ('clockUncertaintyMs', -1), ('clockClampMs', 0)]:
            with self.subTest(session=key):
                self.assertFalse(session.is_valid(dict(fixture, tracks=[dict(fixture['tracks'][0], **{key: value})])))
        case = next(c for c in json.loads((ROOT / 'conformance/vectors.json').read_text()) if c['name'] == 'track-clock-and-document-fields')
        declared = json.loads(case['requests'][1])
        self.assertLessEqual({'clockMethod', 'clockUncertaintyMs', 'clockClampMs', 'documentGeneration'}, set(declared))
        self.assertEqual(case['expect'][1], {'type': 'ack'})
        self.assertIsNone(case['tracks']['x']['documentGeneration'], 'documentGeneration is never written to session.json')
        # Review of D61: hosts copy the three clock facts into session.json as received.
        self.assertEqual({key: case['tracks']['x'].get(key) for key in ('clockMethod', 'clockUncertaintyMs', 'clockClampMs')},
                         {key: declared[key] for key in ('clockMethod', 'clockUncertaintyMs', 'clockClampMs')})

    def test_hosts_validate_each_clock_fact_on_its_own(self):
        # Mac review of #39: an invalid fact is omitted and the valid ones are kept,
        # and hosts copy a clamp above the uncertainty as received.
        request = schema('request')
        cases = {c['name']: c for c in json.loads((ROOT / 'conformance/vectors.json').read_text())}
        case = cases['track-invalid-clock-facts-are-omitted']
        self.assertEqual(case['state'], 'completed')
        tracks = [json.loads(raw) for raw in case['requests'] if json.loads(raw)['type'] == 'track']
        invalid, kept = {key: [] for key in CLOCK_FACTS}, {key: [] for key in CLOCK_FACTS}
        for track, expect in zip(tracks, case['expect'][1:]):
            self.assertEqual(expect, {'type': 'ack'})
            for key in CLOCK_FACTS:
                valid = request.is_valid(dict(type='track', id='x', kind='remote', sampleRate=48000, channels=1, **{key: track[key]}))
                # JSON Schema reads 9.0 and 1e3 as integers; the SPEC allows integer literals only.
                valid = valid and (key == 'clockMethod' or type(track[key]) is int)
                if valid and track[key] is not None:
                    kept[key].append(track[key])
                    self.assertEqual(case['tracks'][track['id']][key], track[key], (track['id'], key))
                else:
                    invalid[key].append(track[key])
                    self.assertIsNone(case['tracks'][track['id']][key], (track['id'], key))
        self.assertEqual(invalid, {'clockMethod': ['sampleClock', 123, None], 'clockUncertaintyMs': [-1, 1.5, 86400001, '9', 9.0],
                                   'clockClampMs': [0, -3, 1.5, 86400001, 1000.0]})
        self.assertTrue(all(kept.values()), 'every fact has a kept valid value next to invalid ones')
        cross = cases['track-clock-facts-are-not-cross-checked']
        declared = json.loads(cross['requests'][1])
        request.validate(declared)
        self.assertGreater(declared['clockClampMs'], declared['clockUncertaintyMs'])
        self.assertEqual({key: cross['tracks']['x'][key] for key in CLOCK_FACTS}, {key: declared[key] for key in CLOCK_FACTS})
        self.assertIn('Hosts do not cross-check the facts', (ROOT / 'SPEC.md').read_text())

    def test_document_generation_bytes_are_checked_by_the_receiver(self):
        # JSON Schema counts characters: 200 two-byte characters pass the schema's
        # outer bound but are 400 UTF-8 bytes, so the receiver (the watchdog oracle
        # and the apps) treats them as absent. Like captureId, the comment says so.
        request = schema('request')
        track = dict(type='track', id='x', kind='remote', sampleRate=48000, channels=1)
        wide = '\u00e9' * 200
        self.assertEqual((len(wide), len(wide.encode())), (200, 400))
        request.validate(dict(track, documentGeneration=wide))
        self.assertIn('At most 200 UTF-8 bytes, independently of this character bound: the receiver checks',
                      (ROOT / 'schemas/request.schema.json').read_text())
        namespace = {}
        oracle = ROOT / 'conformance/multistream/stale_watchdog.py'
        exec(compile(oracle.read_text(), str(oracle), 'exec'), namespace)
        self.assertFalse(namespace['valid'](wide))
        self.assertTrue(namespace['valid']('\u00e9' * 100))
        watchdog = json.loads((ROOT / 'conformance/multistream/stale-watchdog.json').read_text())
        case = next(c for c in watchdog['cases'] if c['name'] == 'a-generation-over-200-utf8-bytes-counts-as-absent')
        self.assertIn(wide, [f.get('documentGeneration') for f in case['frames']])
        self.assertIn('the receiver checks', (ROOT / 'SPEC.md').read_text())

    def test_shared_extension_is_platform_neutral(self):
        for path in (ROOT/'extension').rglob('*'):
            if path.is_file(): self.assertIsNone(re.search(rb'\bmac\b|macos',path.read_bytes(),re.I),str(path))

    def test_platform_topology_fixtures_validate_and_forbid_sensitive_fields(self):
        topology = schema('platform-topology')
        fixtures = sorted((ROOT / 'tests/fixtures/platforms').glob('*.json'))
        self.assertTrue(fixtures, 'at least one platform topology fixture is checked in')
        for path in fixtures:
            with self.subTest(fixture=path.name):
                self.assertRegex(path.name, r'^[a-z0-9]+(?:-[a-z0-9]+)*-\d{4}-\d{2}-\d{2}\.json$')
                value = json.loads(path.read_text())
                topology.validate(value)
                self.assertEqual(path.name, f"{value['platform']}-{value['capturedOn']}.json")

        teams = json.loads((ROOT / 'tests/fixtures/platforms/ms-teams-2026-11-12.json').read_text())
        for mutate in (
            lambda f: f.__setitem__('sdp', 'v=0'),
            lambda f: f['peers'][0]['transceivers'][0]['receiver'].__setitem__('label', 'Chris'),
            lambda f: f['peers'][0]['transceivers'][0]['sender'].__setitem__('label', "Silvio's AirPods"),
            lambda f: f['peers'][0]['transceivers'][0]['sender'].__setitem__('webAudioDestNodeId', None),
            lambda f: f['peers'][0]['transceivers'][0]['sender'].update({'webAudioGraphId': None, 'webAudioDestNodeId': 'n3'}),
            lambda f: f['webAudioGraphs'][0]['edges'].append({'from': 'n1', 'to': 'n2', 'kind': 'audio'}),
            lambda f: f['webAudioGraphs'][0]['edges'][0].__setitem__('output', 0),
            lambda f: f['webAudioGraphs'][0]['nodes'].append({'id': 'n4', 'type': 'ChannelMergerNode', 'inputs': 0}),
            lambda f: f['webAudioGraphs'][0]['nodes'][0].__setitem__('type', 'CustomSecretWorklet'),
            lambda f: f['peers'][0]['transceivers'][0]['receiver'].update({'mixing': 'single', 'speakersOnTrack': 2}),
            lambda f: f.__setitem__('platform', 'https://teams.microsoft.com'),
        ):
            bad = json.loads(json.dumps(teams))
            mutate(bad)
            self.assertFalse(topology.is_valid(bad), bad)

if __name__=='__main__': unittest.main()
