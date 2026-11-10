# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harness acquisition stalls stop before test execution, with useful logs."""

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "build-aux/ci/docker-build-bounded.sh"
IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def builder(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['BUILD_STATE'])
(state / 'calls.json').write_text(json.dumps(sys.argv[1:]))
(state / 'pid').write_text(str(os.getpid()))
(state / 'watchdog').write_text(str(os.getppid()))
print('APT mirror: http://security.example.invalid/ubuntu', file=sys.stderr, flush=True)
mode = os.environ.get('BUILD_MODE', 'success')
if mode == 'failure':
    raise SystemExit(100)
(state / 'input').write_text(sys.stdin.read())
Path(sys.argv[sys.argv.index('--iidfile') + 1]).write_text('sha256:' + 'a' * 64 + '\\n')
os.write(2, b'partial-no-newline')
print('Build progress on stdout', flush=True)
"""
    )
    docker.chmod(0o755)
    return dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", BUILD_STATE=str(tmp_path))


def build(env):
    return subprocess.run(
        [str(HELPER), "native audio readiness", "docker", "build", "-q", "--label", "purpose=two words", "-"],
        env=env,
        input="FROM fixture\n",
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_progress_and_mirror_stay_visible_while_stdout_is_only_the_image_id(builder):
    result = build(builder)
    assert result.returncode == 0 and result.stdout == IMAGE + "\n"
    assert "partial-no-newline" in result.stderr
    assert "900s overall" in result.stderr
    assert "Build progress on stdout" in result.stderr and "security.example.invalid" in result.stderr
    assert "native audio readiness" in result.stderr and "180s" in result.stderr
    state = Path(builder["BUILD_STATE"])
    args = json.loads((state / "calls.json").read_text())
    assert "-q" not in args and "--progress=plain" in args
    assert args[args.index("--label") + 1] == "purpose=two words"
    assert (state / "input").read_text() == "FROM fixture\n"
    assert not Path(args[args.index("--iidfile") + 1]).parent.exists()


def test_failed_build_preserves_exit_status_and_names_the_build_and_mirror(builder):
    result = build(dict(builder, BUILD_MODE="failure"))
    assert result.returncode == 100 and result.stdout == ""
    assert "Harness build [native audio readiness] failed (exit 100)" in result.stderr
    assert "security.example.invalid" in result.stderr and "tests were not run" in result.stderr


@pytest.mark.parametrize(
    "name,limit",
    [
        ("CASSINI_HARNESS_BUILD_TIMEOUT", "0"),
        ("CASSINI_HARNESS_BUILD_TIMEOUT", "901"),
        ("CASSINI_HARNESS_BUILD_TIMEOUT", "-1"),
        ("CASSINI_HARNESS_BUILD_TIMEOUT", "1s"),
        ("CASSINI_HARNESS_BUILD_STALL_TIMEOUT", "301"),
        ("CASSINI_HARNESS_BUILD_STALL_TIMEOUT", "0"),
    ],
)
def test_invalid_budget_does_not_start_a_build(builder, name, limit):
    result = build(dict(builder, **{name: limit}))
    assert result.returncode == 2 and "whole seconds" in result.stderr
    assert not (Path(builder["BUILD_STATE"]) / "calls.json").exists()


def watchdog():
    spec = importlib.util.spec_from_file_location("docker_build_watchdog", HELPER.with_name("docker_build_watchdog.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Schedule:
    def __init__(self, entries):
        self.now = 0
        self.entries = list(entries)

    def poll(self, timeout):
        if self.entries and self.entries[0][0] <= self.now + timeout:
            self.now, event = self.entries.pop(0)
            return [event]
        self.now += timeout
        return []


def assess(entries, stall=180, overall=900):
    module = watchdog()
    events = Schedule(entries)
    output = []
    result = module.supervise(events, module.Progress(0, stall, overall), lambda: events.now, output.append)
    return result, events.now, b"".join(output)


def test_continuous_partial_bytes_allow_a_cold_build_longer_than_old_total_limit():
    entries = [(value, ("output", b".")) for value in (100, 200, 300, 400)]
    result, logical, output = assess([*entries, (401, ("exit", 0)), (401, ("eof", None))])
    assert result == (0, None) and logical == 401 and output == b"...."


def test_stalled_output_expires_from_last_byte_even_without_a_newline():
    result, logical, output = assess([(100, ("output", b"partial")), (200, ("output", b""))])
    assert result == (None, "silence") and logical == 280 and output == b"partial"


def test_overall_limit_stops_an_endless_stream_of_output():
    result, logical, _ = assess([(value, ("output", b".")) for value in range(60, 1200, 60)])
    assert result == (None, "overall") and logical == 900


@pytest.mark.parametrize("first", [("eof", None), ("exit", 0)])
def test_neither_output_eof_nor_parent_exit_alone_means_the_build_is_done(first):
    assert assess([(10, first)])[0] == (None, "silence")


def test_completed_failure_preserves_its_status_and_drains_last_bytes():
    result, _, output = assess([(1, ("exit", 100)), (2, ("output", b"mirror failure")), (3, ("eof", None))])
    assert result == (100, None) and output == b"mirror failure"


@pytest.mark.parametrize("stubborn", [False, True])
def test_stop_signals_only_the_owned_group_and_escalates_after_grace(stubborn):
    module = watchdog()
    calls = []

    class Process:
        pid = 4321

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if stubborn and timeout is not None:
                raise subprocess.TimeoutExpired("owned build", timeout)
            return 0

    escalated = module.stop(
        Process(),
        send=lambda pid, sig: calls.append((pid, sig)),
        pause=lambda seconds: calls.append(("grace", seconds)),
        clock=lambda: 0,
    )
    assert escalated is stubborn
    assert calls[0] == (4321, signal.SIGTERM) and (4321, signal.SIGKILL) in calls
    assert calls.index((4321, signal.SIGKILL)) > calls.index(("wait", 10))
    if not stubborn:
        assert ("grace", 10) in calls


def test_cancellation_after_ready_terminates_the_owned_docker_child(builder):
    # The existing outer timeout only guards a missing readiness/exit event.
    with subprocess.Popen(
        ["timeout", "--kill-after=2", "15", str(HELPER), "cancelled build", "docker", "build", "-"],
        env=builder,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        observed = b""
        while b"APT mirror:" not in observed:
            line = process.stderr.readline()
            assert line, observed
            observed += line
        state = Path(builder["BUILD_STATE"])
        os.kill(int((state / "watchdog").read_text()), signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 143 and stdout == b"" and b"cancelled by signal 15" in stderr, (
        process.returncode,
        stdout,
        stderr,
    )
    with pytest.raises(ProcessLookupError):
        os.kill(int((state / "pid").read_text()), 0)


def test_every_local_harness_build_uses_the_named_deadline():
    builds = []
    paths = [
        *(ROOT / "tests").glob("*/run.sh"),
        ROOT / "tests/whistle/refresh-image.sh",
        ROOT / ".github/workflows/whistle.yml",
    ]
    for path in paths:
        for line in path.read_text().splitlines():
            if re.search(r"\bdocker build\s", line):
                builds.append((path, line))
                assert re.search(r'docker-build-bounded\.sh"? "[^"\n]+" docker build ', line), (path, line)
    assert len(builds) == 13


@pytest.mark.parametrize("reason", ["silence", "overall"])
@pytest.mark.parametrize("escalated", [False, True])
def test_cli_deadline_diagnostics_preserve_timeout_exit_codes(monkeypatch, capsys, reason, escalated):
    from types import SimpleNamespace

    module = watchdog()
    closed, stopped = [], []
    child = SimpleNamespace(pid=4321)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(module, "ProcessEvents", lambda _: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(module, "supervise", lambda *args: (None, reason))
    monkeypatch.setattr(module, "stop", lambda process: stopped.append(process.pid) or escalated)
    assert module.main(["fixture", "docker", "build", "-"]) == (137 if escalated else 124)
    output = capsys.readouterr()
    assert not output.out and "Harness build [fixture]" in output.err and "tests were not run" in output.err
    assert ("no output progress for 180s" if reason == "silence" else "900s overall limit") in output.err
    assert closed == [True] and stopped == [4321]


def test_parent_exit_and_descendant_cleanup_share_one_grace_budget():
    module = watchdog()
    logical, pauses = [0], []

    class Process:
        pid = 4321

        def wait(self, timeout=None):
            if timeout is not None:
                logical[0] = 7
            return 0

    module.stop(Process(), send=lambda *_: None, pause=pauses.append, clock=lambda: logical[0])
    assert pauses == [3]


def test_cancellation_reaps_the_build_before_cleaning_its_iid_directory(monkeypatch, capsys):
    from types import SimpleNamespace

    module = watchdog()
    original = module.tempfile.TemporaryDirectory
    order = []

    class Workspace:
        def __init__(self, **kwargs):
            self.inner = original(**kwargs)
            self.name = self.inner.name

        def __enter__(self):
            return self.name

        def __exit__(self, *_args):
            self.cleanup()

        def cleanup(self):
            assert order == ["reaped", "output closed"], order
            order.append("receipt removed")
            self.inner.cleanup()

    def cancelled(*_args):
        raise module.Interrupted(signal.SIGTERM)

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", Workspace)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=4321))
    monkeypatch.setattr(module, "ProcessEvents", lambda _: SimpleNamespace(close=lambda: order.append("output closed")))
    monkeypatch.setattr(module, "supervise", cancelled)
    monkeypatch.setattr(module, "stop", lambda _: order.append("reaped") or False)
    assert module.main(["receipt race", "docker", "build", "-"]) == 143
    assert order == ["reaped", "output closed", "receipt removed"]
    assert "cancelled by signal 15" in capsys.readouterr().err


def test_cancellation_during_spawn_still_records_and_reaps_the_returned_child(monkeypatch, capsys):
    from types import SimpleNamespace

    module = watchdog()
    order = []
    child = SimpleNamespace(pid=4321)

    def spawn(*args, **kwargs):
        # Model a signal between fork/exec and Popen returning the owned PID.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "ProcessEvents", lambda _: SimpleNamespace(close=lambda: order.append("closed")))
    monkeypatch.setattr(module, "stop", lambda process: order.append(process.pid) or False)
    assert module.main(["spawn cancel", "docker", "build", "-"]) == 143
    assert order == [4321, "closed"]
    assert "cancelled by signal 15" in capsys.readouterr().err
