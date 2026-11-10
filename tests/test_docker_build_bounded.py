# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harness acquisition stalls stop before test execution, with useful logs."""

import json
import os
import re
import subprocess
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
import json, os, signal, sys
from pathlib import Path
state = Path(os.environ['BUILD_STATE'])
(state / 'calls.json').write_text(json.dumps(sys.argv[1:]))
(state / 'pid').write_text(str(os.getpid()))
print('APT mirror: http://security.example.invalid/ubuntu', file=sys.stderr, flush=True)
mode = os.environ.get('BUILD_MODE', 'success')
if mode == 'hang':
    def stopped(*_):
        print('Build client received TERM', file=sys.stderr, flush=True)
        raise SystemExit(23)
    signal.signal(signal.SIGTERM, stopped)
    signal.pause()
if mode == 'failure':
    raise SystemExit(100)
(state / 'input').write_text(sys.stdin.read())
Path(sys.argv[sys.argv.index('--iidfile') + 1]).write_text('sha256:' + 'a' * 64 + '\\n')
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
        timeout=5,
    )


def test_progress_and_mirror_stay_visible_while_stdout_is_only_the_image_id(builder):
    result = build(builder)
    assert result.returncode == 0 and result.stdout == IMAGE + "\n"
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


def test_real_wall_clock_deadline_stops_a_client_waiting_on_an_event(builder):
    result = build(dict(builder, BUILD_MODE="hang", CASSINI_HARNESS_BUILD_TIMEOUT="1"))
    assert result.returncode == 124 and result.stdout == ""
    assert "Build client received TERM" in result.stderr
    assert "native audio readiness] exceeded its 1s wall-clock budget" in result.stderr
    assert "tests were not run" in result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int((Path(builder["BUILD_STATE"]) / "pid").read_text()), 0)


@pytest.mark.parametrize("limit", ["0", "301", "-1", "1s", ""])
def test_invalid_budget_does_not_start_a_build(builder, limit):
    result = build(dict(builder, CASSINI_HARNESS_BUILD_TIMEOUT=limit))
    # An empty override intentionally uses the documented default.
    if limit == "":
        assert result.returncode == 0 and "180s" in result.stderr
    else:
        assert result.returncode == 2 and "whole seconds from 1 to 300" in result.stderr
        assert not (Path(builder["BUILD_STATE"]) / "calls.json").exists()


def test_every_local_harness_build_uses_the_named_deadline():
    builds = []
    for path in (ROOT / "tests").glob("*/run.sh"):
        for line in path.read_text().splitlines():
            if re.search(r"\bdocker build\s", line):
                builds.append((path, line))
                assert re.search(r'docker-build-bounded\.sh"? "[^"\n]+" docker build ', line), (path, line)
    assert len(builds) == 15
