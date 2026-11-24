"""A test that un-mutes Chromium plays real tones: it must refuse to run without a private Pulse sink."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AudioIsolationTests(unittest.TestCase):
    def test_every_unmuted_browser_test_requires_a_private_sink(self):
        offenders = []
        for path in sorted([*(ROOT / 'tests').glob('*.mjs'), *(ROOT / 'scenarios').glob('*.mjs')]):
            text = path.read_text()
            if re.search(r"ignoreDefaultArgs\s*:\s*\[[^\]]*--mute-audio", text) and 'CASSINI_ISOLATED_AUDIO' not in text and 'requirePrivateAudio()' not in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], 'these tests un-mute Chromium without requiring the private audio harness')

    def test_nothing_launches_a_visible_unmuted_browser_by_default(self):
        for path in sorted((ROOT / 'tests').glob('*.mjs')):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if 'headless: false' in line or 'headless:false' in line:
                    self.assertIn('rtc-topology-probe', path.name, f'{path.name}:{number} opens a visible browser')

    def test_helper_refuses_a_desktop_sound_server(self):
        import subprocess
        script = "import {requirePrivateAudio} from './tests/support/private-audio.mjs';" \
                 "for (const env of [{}, {CASSINI_ISOLATED_AUDIO:'1'}, {CASSINI_ISOLATED_AUDIO:'1',PULSE_SERVER:'unix:/run/user/1000/pulse/native'}]) { try { requirePrivateAudio(env); console.log('ACCEPTED'); } catch { console.log('refused'); } }" \
                 "console.log(requirePrivateAudio({CASSINI_ISOLATED_AUDIO:'1',PULSE_SERVER:'unix:/tmp/cassini-audio-x/pulse.sock'}));"
        out = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[:3], ['refused'] * 3)
        self.assertEqual(out[3], 'unix:/tmp/cassini-audio-x/pulse.sock')


if __name__ == '__main__':
    unittest.main()
