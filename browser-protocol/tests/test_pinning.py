# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from verify import verify

class PinningTests(unittest.TestCase):
    def test_committed_stop_mapping_can_replace_an_installed_0215_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 15, 0),
                           'D61.1 Stop uses the committed clock: installers never replace equal 0.2.15')

    def test_steady_drift_can_replace_an_installed_0214_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 14, 0),
                           'D61.1 lets steady drift follow the capture clock: local installers never replace equal 0.2.14')

    def test_render_stall_reanchor_can_replace_an_installed_0213_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 13, 0),
                           'D61.1 re-anchors a stalled capture clock: local installers never replace equal 0.2.13')

    def test_real_time_capture_can_replace_an_installed_0212_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 12, 0),
                           'D68.1 keeps each capture context on real time: local installers never replace equal 0.2.12')

    def test_clock_and_document_facts_can_replace_an_installed_0211_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 11, 0),
                           'D61 clock facts and track documentGeneration need a newer version: local installers never replace equal 0.2.11')

    def test_resume_request_id_can_replace_an_installed_0210_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 10, 0),
                           'The resume requestId needs a newer version: local installers never replace equal 0.2.10')

    def test_result_codes_can_replace_an_installed_029_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 9, 0),
                           'D58 result codes need a newer version: local installers never replace equal 0.2.9')

    def test_source_retirement_fix_can_replace_an_installed_027_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 7, 0))

    def test_resume_feature_can_replace_an_installed_026_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 6, 0),
                           'R67 needs a newer version: local installers never replace equal 0.2.6')

    def test_reload_fix_can_replace_an_installed_022_companion(self):
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        parts = tuple(int(part) for part in manifest['version'].split('.'))
        self.assertGreater(parts + (0,) * (4 - len(parts)), (0, 2, 2, 0),
                           'D39 installers skip equal versions; existing 0.2.2 installs must receive the reload fix')

    def test_vendored_extension_runs_its_node_suite(self):
        commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        with tempfile.TemporaryDirectory() as temp:
            destination, extension = pathlib.Path(temp) / 'browser-protocol', pathlib.Path(temp) / 'BrowserExtension'
            subprocess.run([sys.executable, str(ROOT / 'vendor.py'), commit, str(destination), str(extension)], check=True, capture_output=True)
            scripts = sorted(str(path) for path in (extension / 'tests').glob('*.test.cjs'))
            result = subprocess.run(['node', '--test', *scripts], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'CASSINI_TEST_NATIVE_HOST_FIXTURE': str(ROOT / 'tests/browser_host_fixture.py')})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_exact_commit_and_mutations(self):
        commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        with tempfile.TemporaryDirectory() as temp:
            destination, extension = pathlib.Path(temp) / 'protocol', pathlib.Path(temp) / 'extension'
            subprocess.run([sys.executable, str(ROOT / 'vendor.py'), commit, str(destination), str(extension)], check=True, capture_output=True)
            verify(destination, extension, True)
            (destination / 'PINNED_COMMIT').write_text('0' * 40 + '\n')
            with self.assertRaisesRegex(AssertionError, 'PINNED_COMMIT'):
                verify(destination, extension, True)
            (destination / 'PINNED_COMMIT').write_text(commit + '\n')
            spec = destination / 'SPEC.md'; spec.write_text(spec.read_text() + '\nModified locally.\n')
            checksums = destination / 'SHA256SUMS'
            checksums.write_text('\n'.join(hashlib.sha256(spec.read_bytes()).hexdigest() + '  SPEC.md' if line.endswith('  SPEC.md') else line for line in checksums.read_text().splitlines()) + '\n')
            with self.assertRaisesRegex(AssertionError, 'pinned upstream Git tree'):
                verify(destination, extension, True)

if __name__ == '__main__': unittest.main()
