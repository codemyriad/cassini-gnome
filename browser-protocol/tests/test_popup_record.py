"""popup-record-v1: the literal popup Start/Stop exchange matches both control schemas."""
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator
ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / 'conformance/control/popup-record-vectors.json').read_text())


def validator(name):
    return Draft202012Validator(json.loads((ROOT / 'schemas' / f'control-{name}.schema.json').read_text()))


class PopupRecordTests(unittest.TestCase):
    def setUp(self):
        self.schemas = {'app': validator('host'), 'extension': validator('extension')}

    def test_flow_is_schema_valid_and_answers_every_ask_once(self):
        asks, answers = [], []
        for step in VECTORS['flow']:
            with self.subTest(step=step):
                self.assertTrue(self.schemas[step['from']].is_valid(step['message']))
                if step['message']['type'] == 'recordRequest': asks.append(step['message']['requestId'])
                if step['message']['type'] == 'recordRequestResult': answers.append(step['message']['requestId'])
        self.assertEqual(asks, answers)
        self.assertEqual(len(set(asks)), len(asks), 'request IDs are never reused on a connection')
        # The app's own commands never reuse the popup's request IDs.
        commands = [s['message']['requestId'] for s in VECTORS['flow'] if s['message']['type'] == 'command']
        self.assertFalse(set(commands) & set(asks))

    def test_wire_rows(self):
        self.assertEqual(len({row['name'] for row in VECTORS['wire']}), len(VECTORS['wire']))
        for row in VECTORS['wire']:
            with self.subTest(row=row['name']):
                self.assertEqual(self.schemas[row['from']].is_valid(row['message']), row['valid'])

    def test_capability_is_advertised(self):
        source = (ROOT / 'extension/background.js').read_text()
        self.assertIn('"popup-record-v1"', source)
        hello = dict(type='hello', version=1, capabilities=['heartbeat-v1', VECTORS['capability']])
        self.assertTrue(self.schemas['extension'].is_valid(hello))


if __name__ == '__main__':
    unittest.main()
