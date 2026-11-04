# SPDX-License-Identifier: GPL-3.0-or-later
"""E1: bounded-run.sh leaves nothing of a run alive, and no bound accepts a limit
that bounds nothing (GNU timeout 0 never times out)."""

import functools
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BOUNDED = ROOT / "build-aux/bounded-run.sh"
WRAPPERS = sorted(
    path.relative_to(ROOT).as_posix()
    for path in [ROOT / "build-aux/ux-screenshot.sh", *ROOT.glob("tests/*/run.sh")]
    if "CASSINI_RUN_TIMEOUT" in path.read_text()
)
# 2**64 would wrap to 0 in bash arithmetic: a limit has at most 9 digits.
NO_BOUND = ["", "0", "00", "-1", "1.5", "abc", " 5", "5s", "1000000000", "18446744073709551616"]

# The bounded command: like browser_service.ensure_service it starts a process
# in a new session, out of the process group, and that process ignores TERM.
# It reports the detached pid on the inherited descriptor, then ends as told.
COMMAND = r"""
import os, signal, subprocess, sys
ready, ending = int(sys.argv[1]), sys.argv[2]
detached = subprocess.Popen(
    [sys.executable, "-c", "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(flush=True); signal.pause()"],
    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
detached.stdout.readline()
if ending == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(ready, f"{detached.pid}\n".encode())
os.close(ready)
if ending == "done":
    sys.exit(7)
signal.pause()
"""
# Hidden from the sweep's marker scan: a detached process with an empty environment
# (or one that rewrites it for its title, as Chromium does), not dumpable (its /proc
# entries belong to root), that keeps forking and exiting. It holds the descriptor
# until its deadline, so end of file means every generation is gone.
HIDDEN = r"""
import os, signal, subprocess, sys
ready, ending, deadline = int(sys.argv[1]), sys.argv[2], sys.argv[3]
hopper = r'''
import ctypes, os, signal, sys, time
ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE: 0
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while time.monotonic() < float(sys.argv[1]):
    time.sleep(0.02)
    if os.fork():
        os._exit(0)
'''
subprocess.Popen(
    [sys.executable, "-c", hopper, deadline], start_new_session=True, env={}, pass_fds=[ready],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.write(ready, b"ready\n")
os.close(ready)
if ending == "done":
    sys.exit(7)
signal.pause()
"""
# ending -> (limit, bounded-run.sh's status): the command's own status, timeout's
# 124, 137 when the command ignores TERM too, 130 when bounded-run.sh gets INT.
ENDINGS = {"done": (60, 7), "timeout": (2, 124), "stubborn": (2, 137), "interrupt": (60, 130)}


@functools.cache
def scope_missing():
    """Why the scope mechanism cannot run here, or None."""
    result = subprocess.run(
        [str(BOUNDED), "10", "true"],
        env={**os.environ, "CASSINI_BOUNDED_RUN_MECHANISM": "scope"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return None if result.returncode == 0 else result.stderr.strip() or f"exit {result.returncode}"


def started(pid):
    """The process's start time, or None once it has exited (a zombie has too)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except FileNotFoundError:
        return None
    return None if fields[0] in "ZX" else fields[19]


@pytest.mark.parametrize("ending", ENDINGS)
@pytest.mark.parametrize("mechanism", ["sweep", "scope"])
def test_nothing_of_a_run_outlives_it(mechanism, ending):
    if mechanism == "scope" and (reason := scope_missing()):
        pytest.skip(f"no systemd --user scope: {reason}")
    limit, expected = ENDINGS[ending]
    ready, writer = os.pipe()
    env = {**os.environ, "CASSINI_BOUNDED_RUN_MECHANISM": mechanism, "CASSINI_BOUNDED_RUN_KILL_AFTER": "1"}
    run = subprocess.Popen(
        [str(BOUNDED), str(limit), sys.executable, "-c", COMMAND, str(writer), ending],
        pass_fds=[writer],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(writer)
    pid = start = None
    try:
        assert select.select([ready], [], [], 30)[0], "the command never reported its detached process"
        line = os.read(ready, 64).decode()
        assert line.endswith("\n"), f"the command ended before it was ready: {run.communicate(timeout=30)}"
        pid = int(line)
        start = started(pid)
        if mechanism == "scope":
            assert "/cassini-bounded-run-" in Path(f"/proc/{pid}/cgroup").read_text()
        else:
            assert b"\0CASSINI_BOUNDED_RUN_" in b"\0" + Path(f"/proc/{pid}/environ").read_bytes()
        assert os.getsid(pid) == pid
        if ending == "interrupt":
            run.send_signal(signal.SIGINT)
        _, errors = run.communicate(timeout=60)
        assert (run.returncode, errors) == (expected, "")
        assert started(pid) != start, "the detached process outlived the run"
    finally:
        os.close(ready)
        if run.poll() is None:
            run.kill()
            run.wait()
        # Never leak it, and never kill a process that reused its pid.
        if start is not None and started(pid) == start:
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("limit", NO_BOUND)
def test_bounded_run_refuses_a_limit_that_bounds_nothing(limit):
    result = subprocess.run([str(BOUNDED), limit, "true"], capture_output=True, text=True, timeout=30)
    assert (result.returncode, result.stderr) == (
        2,
        "bounded-run.sh: SECONDS must be whole seconds, from 1 to 999999999\n",
    )
    escalation = subprocess.run(
        [str(BOUNDED), "5", "true"],
        env={**os.environ, "CASSINI_BOUNDED_RUN_KILL_AFTER": limit or "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    message = "CASSINI_BOUNDED_RUN_KILL_AFTER must be whole seconds, from 1 to 999999999\n"
    assert (escalation.returncode, escalation.stderr) == (2, message)


@pytest.mark.parametrize("script", WRAPPERS)
def test_every_wrapper_refuses_a_limit_that_bounds_nothing(script, tmp_path):
    # With nothing on PATH, exit 2 and the message show the check comes before
    # any docker, Xvfb or file work.
    bash = shutil.which("bash")
    for limit in [value for value in NO_BOUND if value] + ["5", "999999999"]:
        result = subprocess.run(
            [bash, ROOT / script],
            env={"PATH": str(tmp_path), "CASSINI_RUN_TIMEOUT": limit},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        refused = (result.returncode, result.stderr) == (
            2,
            "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999\n",
        )
        assert refused == (limit not in ("5", "999999999")), (limit, result.returncode, result.stderr)


@pytest.mark.parametrize("ending", ["done", "timeout"])
def test_the_sweep_kills_what_it_cannot_recognise(ending):
    limit, expected = ENDINGS[ending]
    ready, writer = os.pipe()
    env = {**os.environ, "CASSINI_BOUNDED_RUN_MECHANISM": "sweep", "CASSINI_BOUNDED_RUN_KILL_AFTER": "1"}
    deadline = str(time.monotonic() + 30)
    run = subprocess.Popen(
        [str(BOUNDED), str(limit), sys.executable, "-c", HIDDEN, str(writer), ending, deadline],
        pass_fds=[writer],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(writer)
    try:
        assert select.select([ready], [], [], 30)[0] and os.read(ready, 64) == b"ready\n"
        _, errors = run.communicate(timeout=60)
        assert (run.returncode, errors) == (expected, "")
        # Only the run's processes hold the pipe: end of file once none is left.
        assert select.select([ready], [], [], 10)[0], "a hidden process outlived the run"
        assert os.read(ready, 64) == b""
    finally:
        os.close(ready)
        if run.poll() is None:
            run.kill()
            run.wait()


def test_a_process_that_leaves_the_scope_dies_too(tmp_path):
    # A process can move itself out of the scope (a nested bounded run, Chromium's
    # own scope): here the inner run's scope, whose bounded-run.sh ignores TERM and
    # is KILLed by the outer timeout before it can clean up.
    if reason := scope_missing():
        pytest.skip(f"no systemd --user scope: {reason}")
    ready, writer = os.pipe()
    env = {**os.environ, "CASSINI_BOUNDED_RUN_MECHANISM": "scope", "CASSINI_BOUNDED_RUN_KILL_AFTER": "1"}
    inner = f'trap "" TERM; exec "$0" 20 "$1" -c "$2" {writer} stubborn'
    # A file, not a pipe: a process that escaped would hold a pipe open.
    errors = tmp_path / "stderr"
    with errors.open("w") as stderr:
        run = subprocess.Popen(
            [str(BOUNDED), "2", "bash", "-c", inner, BOUNDED, sys.executable, COMMAND],
            pass_fds=[writer],
            env=env,
            stderr=stderr,
        )
    os.close(writer)
    pid = start = None
    try:
        assert select.select([ready], [], [], 30)[0], "the command never reported its detached process"
        pid = int(os.read(ready, 64))
        start = started(pid)
        assert (run.wait(timeout=60), errors.read_text()) == (137, "")
        assert started(pid) != start, "the detached process outlived the run"
    finally:
        os.close(ready)
        if run.poll() is None:
            run.kill()
            run.wait()
        if start is not None and started(pid) == start:
            os.kill(pid, signal.SIGKILL)
