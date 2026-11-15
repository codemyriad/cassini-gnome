# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1062: the Boards screenshot runs share a few workers without sharing an X display."""

import importlib.util
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("parallel_jobs", ROOT / "build-aux/ci/parallel-jobs.py")
jobs_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jobs_mod)


def _jobs_file(*pairs):
    return b"".join(name.encode() + b"\0" + command.encode() + b"\0" for name, command in pairs)


def test_parse_reads_name_command_pairs_and_rejects_malformed_files():
    assert jobs_mod.parse(_jobs_file(("a", "true"), ("b", "echo 1\necho 2"))) == [
        ("a", "true"),
        ("b", "echo 1\necho 2"),
    ]
    with pytest.raises(ValueError, match="pairs"):
        jobs_mod.parse(b"a\0true\0b\0")
    with pytest.raises(ValueError, match="unique"):
        jobs_mod.parse(_jobs_file(("a", "true"), ("a", "false")))


def test_worker_count_is_bounded_and_validated():
    assert 1 <= jobs_mod.workers({}) <= 4
    assert jobs_mod.workers({"CASSINI_PARALLEL_JOBS": "3"}) == 3
    for bad in ("0", "-1", "two", "65"):
        with pytest.raises(ValueError):
            jobs_mod.workers({"CASSINI_PARALLEL_JOBS": bad})


def test_every_job_runs_after_a_failure_and_each_log_prints_whole(tmp_path):
    out = io.StringIO()
    jobs = [
        ("first", "echo one; echo two >&2"),
        ("broken", "echo before; false; echo never"),
        ("last", "echo three"),
    ]
    assert jobs_mod.run(jobs, 2, tmp_path, out) == 1
    text = out.getvalue()
    for name, lines in (("first", "one\ntwo\n"), ("last", "three\n")):
        assert re.search(rf"::group::{name}: ok in [\d.]+ s\n{lines}::endgroup::\n", text), text
    assert re.search(r"::group::broken: FAILED \(exit 1\) in [\d.]+ s\nbefore\n::endgroup::\n", text), text
    assert "never" not in text, "set -e must stop a job at its first failing command"
    assert text.rstrip().endswith("::error::1 of 3 jobs failed: broken")


def test_concurrent_jobs_get_distinct_display_ranges(tmp_path):
    """Two jobs running at once never start xvfb-run -a from the same display."""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    # Each job announces itself, then waits until both are running.
    wait = (
        f'touch "{barrier}/$CASSINI_XVFB_SERVERNUM"; '
        f'for _ in $(seq 200); do [ "$(ls "{barrier}" | wc -l)" -ge 2 ] && exit 0; sleep 0.05; done; exit 1'
    )
    out = io.StringIO()
    assert jobs_mod.run([("a", wait), ("b", wait)], 2, tmp_path, out) == 0, out.getvalue()
    seen = sorted(int(path.name) for path in barrier.iterdir())
    assert len(seen) == 2 and seen[1] - seen[0] >= jobs_mod.DISPLAYS_PER_SLOT, seen


def test_ux_shots_runs_every_screen_through_the_runner():
    script = (ROOT / "build-aux/ux-shots.sh").read_text()
    assert 'python3 build-aux/ci/parallel-jobs.py "$jobs"' in script
    assert script.index("parallel-jobs.py") < script.index("ux_shots_manifest.py")
    wrapper = (ROOT / "build-aux/ux-screenshot.sh").read_text()
    assert 'xvfb-run -a "${xvfb_from[@]}"' in wrapper and 'xvfb_from=(-n "$CASSINI_XVFB_SERVERNUM")' in wrapper
