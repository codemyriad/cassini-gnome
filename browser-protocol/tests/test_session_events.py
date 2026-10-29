# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
from pathlib import Path
import unittest
from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('session_events', ROOT / 'conformance/check-session-events.py')
annotations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(annotations)


class SessionEventsTests(unittest.TestCase):
    def setUp(self):
        self.session = json.loads((annotations.FIXTURES / 'session.json').read_text())

    def test_both_shared_fixtures_and_materialised_timelines(self):
        annotations.check_fixtures()

    def test_legacy_and_null_annotations_do_not_infer_a_fallback(self):
        for value in [None, []]:
            self.session['events'] = value
            annotations.validate(self.session)
            self.assertIsNone(annotations.state_at(self.session, 960000))
        self.session.pop('events')
        annotations.validate(self.session)
        self.assertIsNone(annotations.state_at(self.session, 960000))

    def test_unknown_event_types_and_extra_fields_are_ignored(self):
        before = annotations.state_at(self.session, 960000)
        self.session['events'].append(dict(atMs=950000, type='future', me=False, source='future-source'))
        self.session['events'][0]['futureField'] = {'anything': True}
        annotations.validate(self.session)
        self.assertEqual(annotations.state_at(self.session, 960000), before)

    def test_known_event_shapes_and_selection_bounds(self):
        for event in [dict(atMs=0, type='selection', me=False, others=False, reason='user'),
                      dict(atMs=0, type='selection', me=1, others=True, reason='start'),
                      dict(atMs=0, type='selection', me=True, reason='start'),
                      dict(atMs=0, type='othersSource', source='microphone', reason='lost'),
                      dict(atMs=0, type='othersSource', source='browser', reason='restored'),
                      dict(atMs=0, type='othersSource', source='system')]:
            with self.subTest(event=event):
                self.session['events'] = [event]
                with self.assertRaises(ValidationError): annotations.validate(self.session)

    def test_order_finite_times_equal_times_and_cap(self):
        self.session['events'] = [dict(atMs=0.125, type='future')] * 1000
        annotations.validate(self.session)
        self.session['events'].append(dict(atMs=0.125, type='future'))
        with self.assertRaises(ValidationError): annotations.validate(self.session)
        for time in [-1, float('nan'), float('inf'), float('-inf')]:
            self.session['events'] = [dict(atMs=time, type='future')]
            with self.assertRaises((ValidationError, ValueError)): annotations.validate(self.session)
        self.session['events'] = [dict(atMs=2, type='future'), dict(atMs=1, type='future')]
        with self.assertRaises(ValueError): annotations.validate(self.session)

    def test_source_identity_has_an_independent_utf8_byte_bound(self):
        track = self.session['tracks'][1]
        for identity in [None, 'x' * 200, '🙂' * 50]:
            track['sourceTrackId'] = identity
            annotations.validate(self.session)
        for identity in ['', 'x' * 201, '🙂' * 51, 42]:
            track['sourceTrackId'] = identity
            with self.assertRaises((ValidationError, ValueError)): annotations.validate(self.session)

    def test_legacy_session_without_source_identity_remains_valid(self):
        fixture = json.loads((ROOT / 'conformance/native-session/session.json').read_text())
        annotations.validate(fixture)


if __name__ == '__main__':
    unittest.main()
