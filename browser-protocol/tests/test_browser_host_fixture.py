import base64
import json
import pathlib
import signal
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class FixtureLifetimeTests(unittest.TestCase):
    def test_dom_provenance_is_persisted_and_reserved_browser_values_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            process = subprocess.Popen([sys.executable, str(ROOT / 'tests/browser_host_fixture.py'), temporary],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                def send(value):
                    payload = json.dumps(value).encode()
                    process.stdin.write(struct.pack('=I', len(payload)) + payload); process.stdin.flush()
                    size, = struct.unpack('=I', process.stdout.read(4))
                    return json.loads(process.stdout.read(size))
                directory = pathlib.Path(send(dict(type='start', features=['source-outcomes-v1']))['path'])
                for index, value in enumerate(['dom', 'browser', None]):
                    track = dict(type='track', id=str(index), kind='remote', label='Hypatia', labelSource=value,
                                 sampleRate=48000, channels=1, sourceTrackId='source-'+str(index))
                    self.assertEqual(send(track)['type'], 'ack')
                for value in ['user', 'device', 'recovered', False, {}, []]:
                    error = send(dict(type='track', id='refused', kind='remote', labelSource=value, sampleRate=48000, channels=1))
                    self.assertEqual((error['type'], error['code'], error['scope']), ('error', 'source', 'source'))
                self.assertEqual(send(dict(type='sources', discoveredSources=[dict(sourceTrackId='source-0', kind='remote', label='Hypatia', labelSource='dom')]))['type'], 'ack')
                send(dict(type='stop')); process.stdin.close()
                self.assertEqual(process.wait(3), 0)
                manifest = json.loads((directory/'session.json').read_text())
                self.assertEqual([t['labelSource'] for t in manifest['tracks']], ['dom', 'browser', 'browser'])
                source = next(s for s in manifest['captureOutcome']['sources'] if s['sourceTrackId']=='source-0')
                self.assertEqual((source['label'], source['labelSource']), ('Hypatia', 'dom'))
            finally:
                if process.poll() is None: process.kill()
                process.wait(3); process.stdin.close(); process.stdout.close()

    def test_eof_and_sigterm_both_checkpoint_accepted_audio_as_interrupted(self):
        for ending in ("eof", "sigterm"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as temporary:
                process = subprocess.Popen([sys.executable, str(ROOT / "tests/browser_host_fixture.py"), temporary],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                try:
                    def send(value):
                        data = json.dumps(value).encode()
                        process.stdin.write(struct.pack("=I", len(data)) + data)
                        process.stdin.flush()
                        size, = struct.unpack("=I", process.stdout.read(4))
                        return json.loads(process.stdout.read(size))
                    started = send(dict(type="start", version=1))
                    directory = pathlib.Path(started["path"])
                    send(dict(type="track", id="remote", kind="remote", sampleRate=48000, channels=1))
                    data = bytes([1, 2]) * 96000
                    send(dict(type="audio", id="remote", offsetMs=0, pcm=base64.b64encode(data).decode()))
                    if ending == "eof":
                        process.stdin.close()
                    else:
                        process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(3), 0)
                    manifest = json.loads((directory / "session.json").read_text())
                    self.assertEqual(manifest["state"], "interrupted")
                    track, = manifest["tracks"]
                    self.assertTrue(track["ended"])
                    self.assertEqual(manifest['events'][-1]['type'], 'end')
                    self.assertEqual(manifest['events'][-1]['reason'], 'error')
                    self.assertGreaterEqual(manifest['events'][-1]['atMs'], track['endOffsetMs'])
                    self.assertEqual(track["frames"], 96000)
                    self.assertEqual((directory / track["file"]).read_bytes()[44:], data)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(3)
                    process.stdin.close()
                    process.stdout.close()

if __name__ == "__main__":
    unittest.main()
