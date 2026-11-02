"""P11 heartbeat-v1: the independent oracle reproduces every literal vector."""
import contextlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('heartbeat', ROOT / 'conformance/control/heartbeat.py')
heartbeat = importlib.util.module_from_spec(spec); spec.loader.exec_module(heartbeat)
VECTORS = json.loads((ROOT / 'conformance/control/heartbeat-vectors.json').read_text())


def run(app_class, case):
    app = app_class()
    return [app.step(step) for step in case['steps']]


class HeartbeatTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(VECTORS['constants'], dict(HEARTBEAT_INTERVAL=10000, CONTROL_LOSS_AFTER=25000))
        self.assertEqual((heartbeat.HEARTBEAT_INTERVAL, heartbeat.CONTROL_LOSS_AFTER), (10000, 25000))

    def test_literal_cases(self):
        self.assertEqual(len({case['name'] for case in VECTORS['cases']}), len(VECTORS['cases']))
        frames = {'hello', 'meetings', 'status', 'result', 'pong'}
        for case in VECTORS['cases']:
            with self.subTest(case=case['name']):
                previous = -1
                self.assertEqual(case['steps'][0].get('in'), 'hello')
                for step in case['steps']:
                    self.assertGreaterEqual(step['atMs'], previous); previous = step['atMs']
                    self.assertIn(step.get('in', 'hello'), frames)
                self.assertEqual(run(heartbeat.App, case), [step['expect'] for step in case['steps']])

    def test_wire_replies(self):
        host = Draft202012Validator(json.loads((ROOT / 'schemas/control-host.schema.json').read_text()))
        extension = Draft202012Validator(json.loads((ROOT / 'schemas/control-extension.schema.json').read_text()))
        self.assertTrue(extension.is_valid(dict(type='hello', version=1, capabilities=['multi-stream-v1', 'heartbeat-v1'])))
        for row in VECTORS['wire']:
            with self.subTest(row=row['name']):
                self.assertEqual(heartbeat.pong_for(row['ping']), row['reply'])
                if row['ping']['type'] == 'ping':
                    # The host schema admits exactly the pings that earn a reply.
                    self.assertEqual(host.is_valid(row['ping']), row['reply'] is not None)
                if row['reply'] is not None:
                    self.assertTrue(extension.is_valid(row['reply']))
        for pong in [dict(type='pong'), dict(type='pong', seq=-1), dict(type='pong', seq='1'), dict(type='pong', seq=2 ** 53)]:
            self.assertFalse(extension.is_valid(pong), pong)

    def test_vectors_reject_plausible_mistakes(self):
        class BeforeStatus(heartbeat.App):  # starts pinging at the hello, not the first status
            def step(self, row):
                actions = heartbeat.App.step(self, row)
                if row.get('in') == 'hello' and self.negotiated:
                    self.active, self.last_inbound, self.last_ping = True, row['atMs'], row['atMs']
                return actions

        class PongOnly(heartbeat.App):  # only a pong proves liveness once active
            def step(self, row):
                was_active = self.connected and self.active
                before = getattr(self, 'last_inbound', None)
                actions = heartbeat.App.step(self, row)
                if was_active and row.get('in') in ('meetings', 'status', 'result'):
                    self.last_inbound = before
                    actions = heartbeat.App.step(self, dict(row, **{'in': None})) if not actions else actions
                return actions

        class IgnoresCapability(heartbeat.App):  # pings any peer
            def step(self, row):
                if row.get('in') == 'hello':
                    row = dict(row, capabilities=['heartbeat-v1'])
                return heartbeat.App.step(self, row)

        mutants = [(name, cls, {}) for name, cls in [('before-status', BeforeStatus), ('pong-only', PongOnly), ('ignores-capability', IgnoresCapability)]]
        mutants += [('loss-one-ms-early', heartbeat.App, dict(CONTROL_LOSS_AFTER=24999)),
                    ('loss-one-ms-late', heartbeat.App, dict(CONTROL_LOSS_AFTER=25001)),
                    ('ping-one-ms-late', heartbeat.App, dict(HEARTBEAT_INTERVAL=10001))]
        for name, cls, constants in mutants:
            with self.subTest(mutant=name), patch.multiple(heartbeat, **constants) if constants else contextlib.nullcontext():
                self.assertTrue(any(run(cls, case) != [step['expect'] for step in case['steps']] for case in VECTORS['cases']))

if __name__ == '__main__':
    unittest.main()
