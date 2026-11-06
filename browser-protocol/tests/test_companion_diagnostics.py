"""Diagnostic fields stay additive to version-1 hello and Stop messages."""
import copy
import json
import pathlib
import unittest
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / 'conformance/control/companion-diagnostics-vectors.json').read_text())

class CompanionDiagnosticsTests(unittest.TestCase):
    def test_hellos_and_legacy_unknown_fields(self):
        schema = json.loads((ROOT / 'schemas/control-extension.schema.json').read_text())
        validator = Draft202012Validator(schema)
        legacy = copy.deepcopy(schema)
        hello = next(item for item in legacy['oneOf'] if item.get('properties', {}).get('type', {}).get('const') == 'hello')
        for key in ('installType', 'filesOk'):
            hello['properties'].pop(key)
        old_validator = Draft202012Validator(legacy)
        for vector in VECTORS['hellos']:
            validator.validate(vector['hello'])
            old_validator.validate(vector['hello'])
        healthy = VECTORS['hellos'][0]['hello']
        for key, invalid in [('filesOk', 'true'), ('filesOk', None), ('installType', 'unpacked'), ('installType', 1)]:
            self.assertFalse(validator.is_valid(dict(healthy, **{key: invalid})))

    def test_stop_cause_is_optional_bounded_and_open_to_future_codes(self):
        schema = json.loads((ROOT / 'schemas/request.schema.json').read_text())
        validator = Draft202012Validator(schema)
        legacy = copy.deepcopy(schema)
        stop = next(item for item in legacy['oneOf'] if item.get('properties', {}).get('type', {}).get('const') == 'stop')
        stop['properties'].pop('cause')
        stop.pop('allOf')
        old_validator = Draft202012Validator(legacy)
        for vector in VECTORS['stops']:
            with self.subTest(vector=vector['name']):
                validator.validate(vector['packet'])
                old_validator.validate(vector['packet'])
        packet = VECTORS['stops'][0]['packet']
        for cause in ['', 'x' * 81, 'frame error', 'frameError\n', None, 1]:
            self.assertFalse(validator.is_valid(dict(packet, cause=cause)))
        self.assertFalse(validator.is_valid(dict(type='stop', cause='frameError')))
        self.assertFalse(validator.is_valid(dict(type='stop', cause='frameError', failureReason='user')))

    def test_diagnostics_can_replace_the_previous_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        self.assertGreater(tuple(map(int, manifest['version'].split('.'))), (0, 2, 17))
        self.assertNotIn('management', manifest['permissions'])
