#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: bounded-run.sh SECONDS COMMAND [ARG...]
# E1: run COMMAND for at most SECONDS (TERM, then KILL CASSINI_BOUNDED_RUN_KILL_AFTER
# seconds later, default 30; exit 124/137, else COMMAND's status; INT, TERM and HUP
# are forwarded as TERM, exit 128 + that signal), then kill everything the run left
# behind: a descendant that called setsid (browser_service.ensure_service does)
# leaves the process group, and one that ignores TERM outlives timeout, which
# escalates only for its child.
# - scope: COMMAND runs in a transient systemd --user scope, killed as a whole when
#   COMMAND returns; RuntimeMaxSec ends it even if this script is killed.
# - sweep, without a user systemd (CI runners, containers): the process group, then
#   every process of this user whose environment carries this run's marker,
#   CASSINI_BOUNDED_RUN_<id>=1, until none is left. A process that clears its
#   environment and leaves the group escapes the sweep.
# CASSINI_BOUNDED_RUN_MECHANISM=auto (default: scope where it works) | scope | sweep.
set -uo pipefail
whole='^0*[1-9][0-9]*$'
[ "$#" -ge 2 ] || { echo "Usage: $0 seconds command [args...]" >&2; exit 2; }
limit=$1
shift
[[ $limit =~ $whole ]] || { echo "bounded-run.sh: SECONDS must be whole seconds, at least 1" >&2; exit 2; }
kill_after=${CASSINI_BOUNDED_RUN_KILL_AFTER:-30}
[[ $kill_after =~ $whole ]] || { echo "CASSINI_BOUNDED_RUN_KILL_AFTER must be whole seconds, at least 1" >&2; exit 2; }
limit=$((10#$limit)) kill_after=$((10#$kill_after))

# The wrappers give the command a private XDG_RUNTIME_DIR: reach the user
# manager through its own socket, and give the command back its environment.
manager=
for dir in "${XDG_RUNTIME_DIR:-}" "/run/user/$UID"; do
  if [ -n "$dir" ] && [ -S "$dir/systemd/private" ]; then manager=$dir; break; fi
done
scope_works() {
  [ -n "$manager" ] && command -v systemd-run >/dev/null &&
    XDG_RUNTIME_DIR=$manager timeout 10 systemd-run --user --scope --quiet --collect true >/dev/null 2>&1
}
mechanism=${CASSINI_BOUNDED_RUN_MECHANISM:-auto}
case "$mechanism" in
  auto) if scope_works; then mechanism=scope; else mechanism=sweep; fi ;;
  scope) scope_works || { echo "bounded-run.sh: no systemd --user scope here" >&2; exit 125; } ;;
  sweep) ;;
  *) echo "CASSINI_BOUNDED_RUN_MECHANISM must be auto, scope or sweep" >&2; exit 2 ;;
esac

# A variable of its own per run, so a nested run keeps the outer run's marker.
id="$$_${RANDOM}${RANDOM}${RANDOM}"
marker="CASSINI_BOUNDED_RUN_$id"
export "$marker=1"
bounded=(timeout --kill-after="$kill_after" "$limit" "$@")
# timeout's own process group no longer gets a terminal's Ctrl-C: forward it.
# Exit 128 + the signal: timeouts disagree (uutils' gave 124 here, GNU's 143).
run='' signalled=0 interrupted=0
# shellcheck disable=SC2317 # called from the traps below
forward() {
  signalled=$1 interrupted=1
  [ -z "$run" ] || kill -TERM "$run" 2>/dev/null
}
trap 'forward 129' HUP
trap 'forward 130' INT
trap 'forward 143' TERM
if [ "$mechanism" = scope ]; then
  unit="cassini-bounded-run-${id//_/-}.scope"
  drop=() keep=()
  for name in XDG_RUNTIME_DIR INVOCATION_ID; do
    if [ -n "${!name+set}" ]; then keep+=("$name=${!name}"); else drop+=(-u "$name"); fi
  done
  # systemd-run --scope execs its command: $! is timeout's pid either way.
  XDG_RUNTIME_DIR=$manager systemd-run --user --scope --quiet --collect --unit="$unit" \
    -p RuntimeMaxSec=$((limit + 2 * kill_after + 10)) -p TimeoutStopSec="$kill_after" \
    -- env "${drop[@]}" "${keep[@]}" "${bounded[@]}" &
else
  # timeout leads its own process group (pgid = its pid).
  "${bounded[@]}" &
fi
run=$!
[ "$signalled" = 0 ] || kill -TERM "$run" 2>/dev/null
while :; do
  interrupted=0 status=0
  # Quiet: no "Killed" notice when timeout ends by KILL (137).
  wait "$run" 2>/dev/null || status=$?
  # A trap ends wait early; wait again for timeout's own status.
  [ "$interrupted" = 1 ] || break
done

marked() {
  local dir entries
  for dir in /proc/[0-9]*; do
    if [ "${dir#/proc/}" = "$$" ] || [ ! -O "$dir" ]; then continue; fi
    # Unreadable once the process has exited, even as a zombie.
    mapfile -t -d '' entries 2>/dev/null <"$dir/environ" || continue
    [[ " ${entries[*]} " == *" $marker=1 "* ]] && echo "${dir#/proc/}"
  done
}
gone=1
if [ "$mechanism" = scope ]; then
  scope() { XDG_RUNTIME_DIR=$manager systemctl --user "$@" 2>/dev/null; }
  scope kill --signal=KILL "$unit"
  # Gone once every member has exited (--collect unloads it).
  for ((pass = 0; pass < 200; pass++)); do
    case $(scope show -P ActiveState "$unit") in active | deactivating | activating | reloading) ;; *) gone=0; break ;; esac
    sleep .05
  done
else
  kill -KILL -- "-$run" 2>/dev/null
  # Each pass kills what the last one missed (a fork in between); bounded.
  for ((pass = 0; pass < 200; pass++)); do
    pids=$(marked)
    [ -z "$pids" ] && { gone=0; break; }
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null
  done
fi
[ "$gone" = 0 ] || echo "bounded-run.sh: processes of this run ($mechanism) are still alive" >&2
[ "$signalled" = 0 ] || status=$signalled
exit "$status"
