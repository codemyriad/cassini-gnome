import hashlib
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from verify import verify

class PinningTests(unittest.TestCase):
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
