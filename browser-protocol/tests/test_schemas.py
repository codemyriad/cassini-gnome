import json
import pathlib
import re
import unittest
from jsonschema import Draft202012Validator, FormatChecker
ROOT = pathlib.Path(__file__).resolve().parent.parent

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
                if expect['type'] in ['started','ack','stopped']: request.validate(json.loads(raw))

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

    def test_shared_extension_is_platform_neutral(self):
        for path in (ROOT/'extension').rglob('*'):
            if path.is_file(): self.assertIsNone(re.search(rb'\bmac\b|macos',path.read_bytes(),re.I),str(path))

if __name__=='__main__': unittest.main()
