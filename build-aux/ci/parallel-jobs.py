#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run independent shell jobs on a few workers, printing each job's log whole.

Usage: parallel-jobs.py JOBS_FILE

JOBS_FILE holds NUL-separated pairs: a job name, then a bash snippet. Jobs
start in file order (put the longest first), each in its own `bash -c` with
`set -euo pipefail`, and with CASSINI_XVFB_SERVERNUM set to the first X
display of its worker slot so concurrent `xvfb-run -a` calls never race for
the same display (D-1062). Every job runs even after a failure, so one run
reports every broken screen; the exit status is 1 if any job failed.

CASSINI_PARALLEL_JOBS sets the worker count (default: half the CPUs, 1 to 4).
"""

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 64 displays per slot: `xvfb-run -a` still skips any display already held.
DISPLAY_BASE, DISPLAYS_PER_SLOT = 200, 64


def parse(data: bytes) -> list[tuple[str, str]]:
    fields = data.decode().split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ValueError("Jobs file must hold name/command pairs")
    jobs = list(zip(fields[::2], fields[1::2], strict=True))
    names = [name for name, _ in jobs]
    if len(set(names)) != len(names) or not all(names):
        raise ValueError("Job names must be unique and non-empty")
    return jobs


def workers(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    value = env.get("CASSINI_PARALLEL_JOBS", "")
    if value:
        if not value.isdigit() or not 1 <= int(value) <= 64:
            raise ValueError("CASSINI_PARALLEL_JOBS must be a whole number from 1 to 64")
        return int(value)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def run(jobs: list[tuple[str, str]], count: int, logs: Path, out=sys.stdout) -> int:
    slots: queue.Queue[int] = queue.Queue()
    for slot in range(count):
        slots.put(slot)
    lock = threading.Lock()
    failed: list[str] = []

    def one(index: int, name: str, command: str) -> None:
        slot = slots.get()
        log = logs / f"{index:03d}.log"
        started = time.monotonic()
        try:
            env = dict(os.environ, CASSINI_XVFB_SERVERNUM=str(DISPLAY_BASE + slot * DISPLAYS_PER_SLOT))
            with log.open("wb") as handle:
                code = subprocess.run(
                    ["bash", "-c", "set -euo pipefail\n" + command],
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    check=False,
                ).returncode
        finally:
            slots.put(slot)
        seconds = time.monotonic() - started
        text = log.read_text(errors="replace")
        with lock:
            status = "ok" if code == 0 else f"FAILED (exit {code})"
            out.write(f"::group::{name}: {status} in {seconds:.1f} s\n{text}")
            if text and not text.endswith("\n"):
                out.write("\n")
            out.write("::endgroup::\n")
            if code:
                failed.append(name)
                out.write(f"::error::{name} failed (exit {code}); its log is in the group above\n")
            out.flush()

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        for future in [pool.submit(one, i, name, command) for i, (name, command) in enumerate(jobs)]:
            future.result()
    out.write(f"{len(jobs)} jobs on {count} workers in {time.monotonic() - started:.1f} s\n")
    if failed:
        out.write(f"::error::{len(failed)} of {len(jobs)} jobs failed: {', '.join(failed)}\n")
    out.flush()
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    jobs = parse(Path(argv[1]).read_bytes())
    with tempfile.TemporaryDirectory(prefix="cassini-parallel-") as logs:
        return run(jobs, workers(), Path(logs))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
