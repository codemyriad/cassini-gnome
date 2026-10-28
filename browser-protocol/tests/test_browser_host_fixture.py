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
