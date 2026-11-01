"""Conformance runs never touch a user Library (drmeyers incident, 2026-11-01).

The fake host resolves its Library like a legacy host: CASSINI_LIBRARY_DIR when
set, otherwise Music/Cassini under the user record's home, never HOME. Each
launcher patches that user record to a temporary home, so the "user" Library
these tests guard is real to the host but never the machine's own.
"""
import contextlib, io, json, os, pathlib, subprocess, sys, tempfile, unittest
from unittest.mock import patch
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'conformance'))
import run as runner

CASES = json.loads((ROOT/'conformance/vectors.json').read_text())
# fake_host implements only the happy-path conversation of the first vector.
PASSING = CASES[0]

def launcher(directory, home):
    executable = pathlib.Path(directory)/'host'
    fake = ROOT/'tests/fake_host.py'
    executable.write_text('#!'+sys.executable+'\nimport pwd,types\n'
                          'pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_dir='+repr(str(home))+')\n'
                          'exec(compile(open('+repr(str(fake))+').read(),'+repr(str(fake))+',"exec"))\n')
    executable.chmod(0o700)
    return executable

def state(folder):
    """Names, sizes and modification times: a create-then-delete still moves mtime."""
    stat = folder.stat()
    return stat.st_mtime_ns, sorted((p.name, p.stat().st_size) for p in folder.rglob('*'))

class LibraryIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp = pathlib.Path(directory.name)
        self.library = self.temp/'home/Music/Cassini'
        self.library.mkdir(parents=True)
        self.host = launcher(self.temp, self.temp/'home')
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start(); self.addCleanup(environment.stop)
        os.environ.pop('CASSINI_LIBRARY_DIR', None); os.environ.pop('CASSINI_BAD_HOST', None)
        quiet = contextlib.ExitStack(); self.addCleanup(quiet.close)
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def test_passing_and_failing_cases_leave_the_user_library_untouched(self):
        self.library.chmod(0o500)  # any host write there fails the case
        self.addCleanup(self.library.chmod, 0o700)
        before = state(self.library)
        self.assertTrue(runner.run(str(self.host), PASSING, self.temp))
        for mode in ('pcm', 'offset', 'path'):
            with self.subTest(mode=mode), patch.dict(os.environ, CASSINI_BAD_HOST=mode):
                self.assertFalse(runner.run(str(self.host), PASSING, self.temp))
        self.assertEqual(state(self.library), before)

    def test_runner_replaces_any_inherited_library_with_a_private_one(self):
        inherited = self.temp/'inherited'
        inherited.mkdir()
        inherited.chmod(0o500)  # any host write there fails the run
        with patch.dict(os.environ, CASSINI_LIBRARY_DIR=str(inherited)):
            self.assertTrue(runner.run(str(self.host), PASSING, self.temp))
        self.assertEqual(list(inherited.iterdir()), [])

    def test_a_host_that_ignores_the_override_stops_the_whole_run(self):
        with patch.dict(os.environ, CASSINI_BAD_HOST='ignore-library'):
            with self.assertRaises(runner.LibraryEscape):
                runner.run(str(self.host), PASSING, self.temp)
        stray = list(self.library.iterdir())
        self.assertEqual(len(stray), 1, 'the stray meeting is reported and kept, not deleted')
        env = dict(os.environ, CASSINI_BAD_HOST='ignore-library', TMPDIR=str(self.temp))
        completed = subprocess.run([sys.executable, str(ROOT/'conformance/run.py'), str(self.host)],
                                   env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('outside its test Library', completed.stderr)
        # The first escape stops the run: one more stray meeting, not one per vector.
        self.assertEqual(len(list(self.library.iterdir())), 2)

    def test_browser_test_host_refuses_to_run_without_a_test_library(self):
        env = {k: v for k, v in os.environ.items() if k != 'CASSINI_LIBRARY_DIR'}
        # Chromium launches a native host with its own origin argument: not a root.
        arguments = ['chrome-extension://pcafdaahfhjiolfpfnlhmljfgoabbaij/', '--parent-window=0']
        completed = subprocess.run([sys.executable, str(ROOT/'tests/browser_host_fixture.py'), *arguments],
                                   env=env, input=b'', capture_output=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b'CASSINI_LIBRARY_DIR', completed.stderr)

if __name__ == '__main__':
    unittest.main()
