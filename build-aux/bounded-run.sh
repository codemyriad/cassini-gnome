#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: bounded-run.sh SECONDS COMMAND [ARG...]
# E1: run COMMAND for at most SECONDS (TERM, then KILL 30 s later; exit 124/137),
# then kill whatever it left behind in its process group, including processes
# that ignore TERM: timeout escalates only while its direct child lives.
set -uo pipefail
[ "$#" -ge 2 ] || { echo "Usage: $0 seconds command [args...]" >&2; exit 2; }
limit=$1
shift
case "$limit" in '' | *[!0-9]*) echo "bounded-run.sh: seconds must be whole" >&2; exit 2 ;; esac
# timeout leads its own process group (pgid = its pid), so a terminal's Ctrl-C
# no longer reaches the command: forward it.
timeout --kill-after=30 "$limit" "$@" &
run=$!
trap 'kill -TERM "$run" 2>/dev/null' INT TERM
status=0
wait "$run" || status=$?
if kill -0 "$run" 2>/dev/null; then
  # Interrupted: wait once more for timeout to forward the signal and return.
  wait "$run" || status=$?
fi
kill -KILL -- "-$run" 2>/dev/null || true
exit "$status"
