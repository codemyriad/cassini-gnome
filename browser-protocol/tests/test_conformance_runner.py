import contextlib, io, json, os, pathlib, sys, tempfile, unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'conformance'))
import run as runner

class RunnerNegativeTests(unittest.TestCase):
    def test_corrupt_hosts_fail_even_when_types_match(self):
        case=json.loads((ROOT/'conformance/vectors.json').read_text())[0]
        fake=ROOT/'tests/fake_host.py'
        with tempfile.TemporaryDirectory() as temp:
            executable=pathlib.Path(temp)/'host'
            executable.write_text('#!'+sys.executable+'\nexec(compile(open('+repr(str(fake))+').read(),'+repr(str(fake))+',"exec"))\n');executable.chmod(0o700)
            for mode in ['', 'title','semantic-title','source','kind','rate','wav-format','offset','pcm','path']:
                with self.subTest(mode=mode), patch.dict(os.environ,CASSINI_FAKE_ROOT=temp,CASSINI_BAD_HOST=mode), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.run(str(executable),case),mode=='')
            with patch.dict(os.environ,CASSINI_FAKE_ROOT=temp,CASSINI_BAD_HOST='keep-alive'),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(runner.run(str(executable),dict(name='must-exit-after-error',requests=['{"type":"other"}'],expect=[dict(type='error',code='version')])))

    def test_post_stop_requires_error_exit_without_eof(self):
        case=next(c for c in json.loads((ROOT/'conformance/vectors.json').read_text()) if c['name']=='request-after-stop')
        fake=ROOT/'tests/fake_host.py'
        with tempfile.TemporaryDirectory() as temp:
            executable=pathlib.Path(temp)/'host'
            executable.write_text('#!'+sys.executable+'\nexec(compile(open('+repr(str(fake))+').read(),'+repr(str(fake))+',"exec"))\n');executable.chmod(0o700)
            for mode in ['', 'keep-alive-after-stop', 'zero-after-stop']:
                with self.subTest(mode=mode), patch.dict(os.environ,CASSINI_FAKE_ROOT=temp,CASSINI_BAD_HOST=mode), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.run(str(executable),case), mode=='')

if __name__=='__main__':unittest.main()
