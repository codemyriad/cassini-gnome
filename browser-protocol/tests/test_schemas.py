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

    def test_shared_extension_is_platform_neutral(self):
        for path in (ROOT/'extension').rglob('*'):
            if path.is_file(): self.assertIsNone(re.search(rb'\bmac\b|macos',path.read_bytes(),re.I),str(path))

if __name__=='__main__': unittest.main()
