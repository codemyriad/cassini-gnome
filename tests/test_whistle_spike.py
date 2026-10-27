# SPDX-License-Identifier: GPL-3.0-or-later
"""Constructor guard and worker lifecycle without downloading any models."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_viewer import whistle_client, whistle_worker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", whistle_worker.PRIVACY)
@pytest.mark.parametrize("value", [None, "unexpected"])
def test_privacy_guard_precedes_native_constructor(tmp_path, name, value):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("Native constructor proof needs a C compiler")
    source = tmp_path / "sentinel.c"
    library = tmp_path / "sentinel.so"
    marker = tmp_path / "constructed"
    source.write_text("""#include <stdio.h>
#include <stdlib.h>
__attribute__((constructor)) static void construct(void) {
  FILE *file=fopen(getenv("CASSINI_SENTINEL"),"w");
  if(file) { fputs("loaded",file); fclose(file); }
}""")
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    script = """import ctypes,sys
from cassini_viewer.whistle_worker import guarded_load
try:
    guarded_load(lambda: ctypes.CDLL(sys.argv[1]))
except RuntimeError:
    sys.exit(42)
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), CASSINI_SENTINEL=str(marker), **whistle_worker.PRIVACY)
    if value is None:
        env.pop(name)
    else:
        env[name] = value
    assert subprocess.run([sys.executable, "-c", script, str(library)], env=env).returncode == 42
    assert not marker.exists(), "Native constructor ran before the privacy guard"
    env.update(whistle_worker.PRIVACY)
    assert subprocess.run([sys.executable, "-c", script, str(library)], env=env).returncode == 0
    assert marker.read_text() == "loaded"


def child_client(tmp_path, monkeypatch, script, timeout=1):
    monkeypatch.setattr(whistle_client, "verify_bundle", lambda bundle: Path(bundle))
    popen = subprocess.Popen

    def launch(_argv, **kwargs):
        kwargs["env"].pop("LD_PRELOAD", None)  # Fake worker needs no native bridge.
        return popen([sys.executable, "-u", "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    return whistle_client.WhistleClient(tmp_path, timeout=timeout)


def test_reuses_one_child_for_two_requests_and_reaps_it(tmp_path, monkeypatch):
    client = child_client(
        tmp_path,
        monkeypatch,
        """import json,os,sys
print(json.dumps(dict(type='ready',pid=os.getpid())))
for line in sys.stdin:
    if json.loads(line)['type']=='quit': break
    print(json.dumps(dict(type='result',pid=os.getpid(),text='same words')))
""",
    )
    with client:
        child = client.process
        one, two = client.transcribe(tmp_path / "a.wav"), client.transcribe(tmp_path / "b.wav")
        assert one == two and one["pid"] == child.pid != os.getpid()
    assert child.poll() == 0 and client.process is None


def test_partial_reply_times_out_and_reaps_worker(tmp_path, monkeypatch):
    client = child_client(
        tmp_path,
        monkeypatch,
        """import sys,time,json
print(json.dumps(dict(type='ready')))
sys.stdin.readline()
sys.stdout.write('{"type":');sys.stdout.flush()
time.sleep(30)
""",
        timeout=0.1,
    )
    with client:
        child = client.process
        with pytest.raises(RuntimeError, match="timed out"):
            client.transcribe(tmp_path / "test.wav")
        assert child.poll() is not None and client.process is None


def test_crashed_worker_is_reported_and_cleanup_is_safe(tmp_path, monkeypatch):
    client = child_client(
        tmp_path,
        monkeypatch,
        """import sys,json
print(json.dumps(dict(type='ready')))
sys.stdin.readline()
sys.exit(7)
""",
    )
    with client:
        child = client.process
        with pytest.raises(RuntimeError, match="exited"):
            client.transcribe(tmp_path / "test.wav")
    assert child.poll() == 7 and client.process is None


def test_bundle_hash_mismatch_is_rejected_before_launch(tmp_path):
    pins = json.loads((ROOT / "whistle/pins.json").read_text())
    (tmp_path / "pins.json").write_text(json.dumps(pins))
    manifest = dict(pins["installed"], **{"libcassini_whistle_cpu.so": "0" * 64})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "whistle.cact").write_bytes(b"corrupted weights")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        whistle_client.WhistleClient(tmp_path)


def test_build_fetch_rejects_corrupt_cached_artifact(tmp_path):
    import runpy

    namespace = runpy.run_path(str(ROOT / "whistle/fetch.py"))
    (tmp_path / "whistle.cact").write_bytes(b"bad cached download")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        namespace["fetch"](tmp_path / "output", tmp_path)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("event", ["socket.sendto", "socket.sendmsg", "socket.getnameinfo", "socket.__new__"])
def test_worker_network_guard_covers_connectionless_operations(event):
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        whistle_worker.block_network(event, ())
