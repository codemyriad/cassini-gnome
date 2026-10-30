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

    def test_shared_fixtures_materialised_timelines_and_reserved_reader_vector(self):
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
                      dict(atMs=0, type='othersSource', source='browser', reason='unknown'),
                      dict(atMs=0, type='selection', me=True, others=True, reason='restored'),
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

    def test_shared_call_lifecycle_vector_oracle(self):
        from contextlib import redirect_stdout
        import io
        spec = importlib.util.spec_from_file_location('call_lifecycle', ROOT/'conformance/check-call-lifecycle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with redirect_stdout(io.StringIO()): module.check()

    def test_terminal_boundary_cannot_precede_accepted_pcm(self):
        session = json.loads((annotations.FIXTURES / 'call-return.session.json').read_text())
        session['events'][-1]['atMs'] = 3000  # Ordered, but finalized PCM runs to 5000.
        with self.assertRaisesRegex(ValueError, 'track end'):
            annotations.validate(session)
        for end in [5000, 5000.125, 6000]:
            session['events'][-1]['atMs'] = end
            annotations.validate(session)
        # An absent event or an unknown/null end is never an inferred boundary.
        session['events'].pop()
        annotations.validate(session)
        for track in session['tracks']:
            track.pop('endOffsetMs', None)
        session['events'].append(dict(type='end', atMs=3000, reason='user'))
        annotations.validate(session)
        for track in session['tracks']:
            track['endOffsetMs'] = None
        annotations.validate(session)

    def test_d33_terminal_end_reasons_and_single_checkpoint_boundary(self):
        self.session['state'] = 'completed'
        end = max(track.get('endOffsetMs') or 0 for track in self.session['tracks'])
        for reason in ['user', 'callEnded', 'tabClosed', 'pageGone', 'disk', 'error', 'appQuit']:
            with self.subTest(reason=reason):
                self.session['events'] = [dict(type='end', atMs=end, reason=reason)]
                annotations.validate(self.session)
        for event in [dict(type='end', atMs=end), dict(type='end', atMs=end, reason='unknown'),
                      dict(type='end', atMs=-1, reason='user')]:
            self.session['events'] = [event]
            with self.assertRaises((ValueError, ValidationError)): annotations.validate(self.session)
        self.session['events'] = [dict(type='end', atMs=end, reason='user')] * 2
        with self.assertRaises(ValidationError): annotations.validate(self.session)
        self.session['events'] = [dict(type='end', atMs=end, reason='user'), dict(type='future', atMs=end)]
        with self.assertRaises(ValueError): annotations.validate(self.session)
        self.session['state'] = 'recording'
        self.session['events'] = [dict(type='end', atMs=end, reason='user')]
        with self.assertRaises(ValueError): annotations.validate(self.session)
        self.session['state'] = 'completed'
        self.session['events'] = [dict(type='future', atMs=0)] * 999 + [dict(type='end', atMs=end, reason='user')]
        annotations.validate(self.session)
        self.session['events'].insert(0, dict(type='future', atMs=0))
        with self.assertRaises(ValidationError): annotations.validate(self.session)

    def test_d33_restore_is_only_browser_after_fallback_with_others_selected(self):
        restore = dict(type='othersSource', atMs=2, source='browser', reason='restored')
        fallback = dict(type='othersSource', atMs=0, source='system', reason='lost')
        self.session['events'] = [fallback, restore]
        annotations.validate(self.session)
        for events in [[restore], [dict(fallback, reason='user'), restore],
                       [fallback, dict(restore, source='system')],
                       [fallback, dict(type='selection',atMs=1,me=True,others=False,reason='user'),restore]]:
            self.session['events'] = events
            with self.assertRaises((ValueError,ValidationError)): annotations.validate(self.session)


if __name__ == '__main__':
    unittest.main()
