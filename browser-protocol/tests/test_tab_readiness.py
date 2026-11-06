"""Additive browser-health contract and hand-written consumer expectations."""
import copy
import json
import pathlib
import unittest
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent

class TabReadinessTests(unittest.TestCase):
    def test_consumer_states_and_nested_results(self):
        schema = json.loads((ROOT / 'schemas/control-extension.schema.json').read_text())
        validator = Draft202012Validator(schema)
        vectors = json.loads((ROOT / 'conformance/control/tab-readiness-vectors.json').read_text())
        self.assertEqual(len(vectors), 6)
        for vector in vectors:
            with self.subTest(vector=vector['name']):
                status = vector['status']
                validator.validate(status)
                validator.validate(dict(type='result', requestId='health', ok=True, status=status))
                health = status['browserHealth']
                self.assertEqual(health['state'] == 'reloadRequired', vector['expectReloadWarning'])
                if health['state'] == 'idle':
                    self.assertFalse(health['tabs'])
                    self.assertFalse(vector['expectReloadWarning'])
                for key, invalid in [('tabs', [{}]), ('tabs', health['tabs'] * 101 or [{}]),
                                     ('state', 'nativeConnected'), ('extensionInstance', ''),
                                     ('extensionVersion', 'x' * 81), ('nativeControl', True)]:
                    bad = copy.deepcopy(status)
                    bad['browserHealth'][key] = invalid
                    self.assertFalse(validator.is_valid(bad), (vector['name'], key, invalid))

    def test_reload_command_is_additive(self):
        validator = Draft202012Validator(json.loads((ROOT / 'schemas/control-host.schema.json').read_text()))
        command = dict(type='command', requestId='reload-2', action='reloadTab', tabId=2, independent=True)
        validator.validate(command)
        self.assertFalse(validator.is_valid(dict(command, tabId=-1)))
