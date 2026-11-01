"""D52 shared outcomes through the framed browser fixture, with bounded events."""
import base64
import contextlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wire', ROOT / 'conformance/run.py')
wire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)


@contextlib.contextmanager
def host():
    with tempfile.TemporaryDirectory() as temporary:
        process = subprocess.Popen([sys.executable, str(ROOT / 'tests/browser_host_fixture.py'), temporary],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def send(value):
            payload = json.dumps(value).encode()
            process.stdin.write(struct.pack('=I', len(payload)) + payload)
            process.stdin.flush()
            return wire.response(process.stdout)
        try:
            started = send(dict(type='start', version=1, captureId='capture-current', features=['source-outcomes-v1']))
            assert started['features'] == ['source-outcomes-v1']
            yield send, Path(started['path']), Path(temporary)
        finally:
            process.stdin.close()
            try:
                process.wait(3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(3)
                raise AssertionError('browser fixture did not exit after EOF') from None
            error = process.stderr.read().decode()
            process.stdout.close(); process.stderr.close()
            assert process.returncode == 0, error


def track(send, identifier, source=None):
    return send(dict(type='track', id=identifier, sourceTrackId=source or identifier,
                     label='Guest', kind='remote', sampleRate=8000, channels=1))


def audio(send, identifier, frames=8000, offset=0, value=0, sequence=0):
    return send(dict(type='audio', id=identifier, sequence=sequence, offsetMs=offset,
                     pcm=base64.b64encode(struct.pack('<h', value) * frames).decode()))


def end(send, identifier, reason='ended'):
    return send(dict(type='trackEnd', id=identifier, reason=reason, droppedFrames=0, endOffsetMs=None))


class MultistreamTests(unittest.TestCase):
    def test_live_source_updates_are_durable_atomic_sticky_and_cannot_rewrite_after_stop(self):
        with host() as (send, folder, _):
            source = dict(sourceTrackId='never-opened', kind='remote', label='Guest', failed=True)
            self.assertEqual(send(dict(type='sources', discoveredSources=[source]))['type'], 'ack')
            saved = json.loads((folder / 'session.json').read_text())
            wire.VALIDATORS['session'].validate(saved)
            self.assertEqual(saved['tracks'], [])
            self.assertEqual(saved['captureOutcome']['sources'][0]['captureOutcome'], 'failed')
            before = (folder / 'session.json').read_bytes()
            invalid = send(dict(type='sources', discoveredSources=[dict(source, sourceTrackId='valid-but-rolled-back'), dict(source, sourceTrackId='é'*101)]))
            self.assertEqual((invalid['type'],invalid['scope']), ('error','command'))
            self.assertEqual((folder / 'session.json').read_bytes(), before)
            send(dict(type='sources', discoveredSources=[dict(source, failed=False)]))
            self.assertEqual(json.loads((folder / 'session.json').read_text())['captureOutcome']['sources'][0]['captureOutcome'], 'failed')
            send(dict(type='stop', captureId='capture-current'))
            before = (folder / 'session.json').read_bytes()
            refused = send(dict(type='sources', discoveredSources=[dict(source, sourceTrackId='too-late')]))
            self.assertEqual((refused['type'],refused['scope'],refused['code']),('error','command','state'))
            self.assertEqual((folder / 'session.json').read_bytes(), before)

    def test_retired_spans_and_finalized_capture_refuse_packets_without_rewriting_files(self):
        with host() as (send, folder, _):
            track(send, 'old', 'root'); audio(send, 'old'); end(send, 'old')
            def snapshot():
                return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
            before = snapshot()
            for request in (dict(type='track', id='old', sourceTrackId='root', kind='remote', sampleRate=8000, channels=1),
                            dict(type='trackEnd', id='old', reason='failed', droppedFrames=99, endOffsetMs=999999)):
                reply = send(request)
                self.assertEqual((reply['type'], reply['code'], reply['scope']), ('error', 'state', 'source'))
                self.assertEqual(snapshot(), before)
            self.assertEqual(audio(send, 'old')['type'], 'error')
            self.assertEqual(snapshot(), before)
            track(send, 'new', 'root'); audio(send, 'new', offset=1000)
            send(dict(type='stop', captureId='capture-current'))
            before = snapshot()
            requests = [dict(type='track', id='late', sourceTrackId='other', kind='remote', sampleRate=8000, channels=1),
                        dict(type='trackEnd', id='new', reason='failed', droppedFrames=99, endOffsetMs=999999),
                        dict(type='audio', id='new', sequence=99, offsetMs=1000, pcm='AAA=')]
            for request in requests:
                reply = send(request)
                self.assertEqual((reply['type'], reply['code'], reply['scope']), ('error', 'state', 'command'))
                self.assertEqual(snapshot(), before)
            metadata = json.loads((folder / 'session.json').read_text())
            self.assertEqual(metadata['captureOutcome']['sources'][0]['captureOutcome'], 'captured')

    def test_hand_written_capture_outcomes(self):
        vectors = json.loads((ROOT / 'conformance/multistream/outcomes.json').read_text())
        for case in vectors['cases']:
            with self.subTest(case=case['name']), host() as (send, folder, _):
                if case.get('announcedSources'):
                    self.assertEqual(send(dict(type='sources', discoveredSources=case['announcedSources']))['type'], 'ack')
                    checkpoint = json.loads((folder / 'session.json').read_text())
                    self.assertEqual(checkpoint['tracks'], [])
                    self.assertEqual({s['sourceTrackId']:s['captureOutcome'] for s in checkpoint['captureOutcome']['sources']}, case['sourceExpected'])
                for item in case['tracks']:
                    self.assertEqual(track(send, item['id'], item['sourceTrackId'])['type'], 'ack')
                for item in case['writes']:
                    self.assertEqual(audio(send, item['id'], item['frames'], item['offsetMs'], item['value'])['type'], 'ack')
                self.assertEqual(send(dict(type='stop', captureId='capture-current',
                                           discoveredSources=case.get('discoveredSources', [])))['type'], 'stopped')
                metadata = json.loads((folder / 'session.json').read_text())
                wire.VALIDATORS['session'].validate(metadata)
                self.assertEqual({t['id']: t['captureOutcome'] for t in metadata['tracks']}, case['expected'])
                self.assertEqual({s['sourceTrackId']: s['captureOutcome'] for s in metadata['captureOutcome']['sources']},
                                 case['sourceExpected'])
                for item in case['tracks']:
                    stored = next(t for t in metadata['tracks'] if t['id'] == item['id'])
                    self.assertEqual(stored['receivedFrames'], sum(w['frames'] for w in case['writes'] if w['id'] == item['id']))

    def test_source_refusal_io_and_wrong_stop_leave_other_audio_alive(self):
        with host() as (send, folder, root):
            track(send, 'a'); track(send, 'b')
            marker = root / 'fail-audio-b'; marker.touch()
            failure = audio(send, 'b')
            self.assertEqual((failure['code'], failure['scope'], failure['id']), ('io', 'source', 'b'))
            self.assertEqual(audio(send, 'a')['type'], 'ack')
            stale = send(dict(type='stop', captureId='old-capture'))
            self.assertEqual((stale['type'], stale['scope']), ('error', 'command'))
            self.assertEqual(json.loads((folder / 'session.json').read_text())['state'], 'recording')
            end(send, 'b', 'failed'); marker.unlink()
            self.assertEqual(track(send, 'b2', 'b')['type'], 'ack')
            self.assertEqual(audio(send, 'b2', offset=1000)['type'], 'ack')
            self.assertEqual(audio(send, 'a', offset=1000, sequence=1)['type'], 'ack')
            send(dict(type='stop', captureId='capture-current'))
            metadata = json.loads((folder / 'session.json').read_text())
            self.assertEqual(metadata['problemKind'], 'io')
            self.assertEqual({s['sourceTrackId']: s['captureOutcome'] for s in metadata['captureOutcome']['sources']},
                             {'a':'captured', 'b':'failed'})

    def test_duplicate_declaration_and_seventy_cycles_do_not_close_the_session(self):
        with host() as (send, folder, _):
            self.assertEqual(track(send, 'a')['type'], 'ack')
            self.assertEqual(track(send, 'a')['type'], 'ack')
            for n in range(70):
                identifier = f'span-{n}'
                reply = track(send, identifier, 'returning-source')
                if n < 63:
                    self.assertEqual(reply['type'], 'ack')
                    audio(send, identifier, frames=400, offset=n * 1000)
                    end(send, identifier)
                else:
                    self.assertEqual((reply['type'], reply['code'], reply['scope']), ('error', 'limit', 'source'))
                self.assertEqual(audio(send, 'a', frames=400, offset=n * 50, sequence=n)['type'], 'ack')
            send(dict(type='stop', captureId='capture-current'))
            metadata = json.loads((folder / 'session.json').read_text())
            self.assertEqual(len(metadata['tracks']), 64)
            self.assertEqual(next(t for t in metadata['tracks'] if t['id'] == 'a')['receivedFrames'], 28000)

    def test_overlapping_live_and_closed_spans_are_refused_without_merging(self):
        with host() as (send, folder, _):
            track(send, 'a', 'same'); audio(send, 'a')
            refused = track(send, 'simultaneous', 'same')
            self.assertEqual((refused['code'], refused['scope']), ('source', 'source'))
            end(send, 'a'); track(send, 'b', 'same')
            refused = audio(send, 'b', offset=500)
            self.assertEqual((refused['code'], refused['scope']), ('source', 'source'))
            self.assertEqual(audio(send, 'b', offset=1000)['type'], 'ack')
            send(dict(type='stop', captureId='capture-current'))
            metadata = json.loads((folder / 'session.json').read_text())
            self.assertEqual([t['frames'] for t in metadata['tracks']], [8000, 8000])
            self.assertEqual(metadata['captureOutcome']['sources'][0]['captureOutcome'], 'captured')

    def test_ninety_second_gap_is_refused_per_source_and_a_new_span_can_resume(self):
        with host() as (send, folder, _):
            track(send, 'a', 'same'); audio(send, 'a')
            refused = audio(send, 'a', offset=90000, sequence=1)
            self.assertEqual((refused['code'], refused['scope']), ('chunk', 'source'))
            end(send, 'a'); track(send, 'after', 'same')
            self.assertEqual(audio(send, 'after', offset=90000)['type'], 'ack')
            send(dict(type='stop', captureId='capture-current'))
            metadata = json.loads((folder / 'session.json').read_text())
            self.assertEqual([t['frames'] for t in metadata['tracks']], [8000, 8000])

    def test_null_source_identity_is_absent_and_utf8_bounds_stay_per_source(self):
        with host() as (send, folder, _):
            nullable = dict(type='track', id='nullable', sourceTrackId=None, label='Guest',
                            kind='remote', sampleRate=8000, channels=1)
            wire.Draft202012Validator(json.loads((ROOT / 'schemas/request.schema.json').read_text())).validate(nullable)
            self.assertEqual(send(nullable)['type'], 'ack')
            rejected = track(send, 'too-many-bytes', '€' * 67)
            self.assertEqual((rejected['code'], rejected['scope']), ('source', 'source'))
            self.assertEqual(track(send, 'valid-utf8', '€' * 66)['type'], 'ack')
            self.assertEqual(audio(send, 'nullable')['type'], 'ack')
            self.assertEqual(audio(send, 'valid-utf8')['type'], 'ack')
            send(dict(type='stop', captureId='capture-current'))
            metadata = json.loads((folder / 'session.json').read_text())
            wire.VALIDATORS['session'].validate(metadata)
            self.assertNotIn('sourceTrackId', metadata['tracks'][0])
            metadata['tracks'][0]['sourceTrackId'] = None
            wire.VALIDATORS['session'].validate(metadata)

    def test_diagnostic_sources_can_exceed_the_audio_track_limit(self):
        # No extra WAVs are allocated: the ledger includes sources refused
        # before track admission. The native framing limit still bounds JSON.
        sources = [dict(sourceTrackId=f'missing-{n}', kind='remote', failed=True) for n in range(257)]
        request = dict(type='stop', captureId='capture-current', discoveredSources=sources)
        wire.Draft202012Validator(json.loads((ROOT / 'schemas/request.schema.json').read_text())).validate(request)
        with host() as (send, folder, _):
            self.assertEqual(send(request)['type'], 'stopped')
            metadata = json.loads((folder / 'session.json').read_text())
            wire.VALIDATORS['session'].validate(metadata)
            self.assertEqual(len(metadata['tracks']), 0)
            self.assertEqual(len(metadata['captureOutcome']['sources']), 257)
            self.assertTrue(all(s['captureOutcome'] == 'failed' for s in metadata['captureOutcome']['sources']))


if __name__ == '__main__':
    unittest.main()
