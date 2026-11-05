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

    def test_an_uncommitted_return_is_true_only_with_the_absence_in_the_file(self):
        """Either writer form passes; a zero-filled loss never does (D47, D52.2)."""
        kept = json.loads((annotations.FIXTURES / 'uncommitted-return-kept.session.json').read_text())
        expected = json.loads((annotations.FIXTURES / 'uncommitted-return-kept.expected.json').read_text())
        tracks = {track['id']: track for track in kept['tracks']}
        one = dict(tracks['remote-before'], id='remote', file='track-remote.wav', frames=40000, endOffsetMs=5000)
        for gaps, present in [
            ([dict(atMs=1000, durationMs=2500, frames=20000, kind='jump')], True),  # One span, the loss as a range.
            ([], False),  # The same span zero-filled: it would claim the Others were there.
        ]:
            session = dict(kept, tracks=[tracks['mic'], dict(one, gaps=gaps)])
            annotations.validate(session)
            self.assertEqual(annotations.others_presence(session) == expected['othersPresenceMs'], present)

    def test_presence_starts_at_the_d55_placement(self):
        """R88 (D67 item 7): 1000.03 ms is 16 kHz sample 16000.48, placed at 16000 (1000 ms).

        The extent lasts its 8000 frames at 8 kHz, so it ends at 2000 ms, not 2000.03 ms;
        the gap [1500, 1600) stays absolute. 1000.03125 ms is 16000.5, placed half away
        from zero at 16001, so its extent is [1000.0625, 2000.0625).
        """
        tracks = {track['id']: track for track in self.session['tracks']}
        remote = dict(tracks['browser-1'], frames=8000, startOffsetMs=1000.03, endOffsetMs=2000.03,
                      gaps=[dict(atMs=1500, durationMs=100, frames=800, kind='jump')])
        session = dict(self.session, tracks=[tracks['microphone-1'], remote])
        self.assertEqual(annotations.others_presence(session), [[1000.0, 1500], [1600, 2000.0]])
        tie = dict(remote, startOffsetMs=1000.03125, endOffsetMs=2000.03125, gaps=[])
        session = dict(self.session, tracks=[tracks['microphone-1'], tie])
        self.assertEqual(annotations.others_presence(session), [[1000.0625, 2000.0625]])

    def test_a_track_with_no_known_start_adds_no_presence(self):
        """startOffsetMs is optional (readers also accept null): a track with no known start has no
        extent, so it adds no presence and never aborts the oracle (a 0-frame declaration that Mac
        commits before its restore)."""
        tracks = {track['id']: track for track in self.session['tracks']}
        remote = dict(tracks['browser-1'], frames=8000, startOffsetMs=1000, endOffsetMs=2000, gaps=[])
        session = dict(self.session, tracks=[tracks['microphone-1'], remote])
        present = annotations.others_presence(session)
        for start in ('missing', None):
            with self.subTest(startOffsetMs=start):
                declared = dict(remote, id='declared', file='track-declared.wav', frames=0, ended=True)
                declared.pop('endOffsetMs')
                declared.pop('startOffsetMs')
                if start is None:
                    declared.update(startOffsetMs=None, endOffsetMs=None)
                with_declared = dict(self.session, tracks=[tracks['microphone-1'], remote, declared])
                annotations.validate(with_declared)
                self.assertEqual(annotations.others_presence(with_declared), present)

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

    def test_wire_vectors_reject_a_reader_that_retains_departed_document_counts(self):
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import io
        spec = importlib.util.spec_from_file_location('call_lifecycle', ROOT/'conformance/check-call-lifecycle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.WirePolicy, 'invalidate', lambda _: None), redirect_stdout(io.StringIO()):
            with self.assertRaises(AssertionError):
                module.check_wire()

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

    def test_restore_after_a_fallback_that_never_committed(self):
        # The call returned before its fallback committed (SPEC-CONTROL "Return before the
        # fallback commits"): none/lost, then browser/restored with no system track between.
        restore = dict(type='othersSource', atMs=2, source='browser', reason='restored')
        lost = dict(type='othersSource', atMs=0, source='none', reason='lost')
        self.session['events'] = [lost, restore]
        annotations.validate(self.session)
        # Only a loss: Others turned off (none/user) is not a fallback, nor is a lost Others side.
        for events in [[dict(lost, reason='user'), restore],
                       [lost, dict(type='selection',atMs=1,me=True,others=False,reason='user'),restore]]:
            self.session['events'] = events
            with self.assertRaises(ValueError): annotations.validate(self.session)


if __name__ == '__main__':
    unittest.main()
