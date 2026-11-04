#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: bounded-run.sh SECONDS COMMAND [ARG...]
# E1: run COMMAND for at most SECONDS (TERM, then KILL CASSINI_BOUNDED_RUN_KILL_AFTER
# seconds later, default 30; exit 124/137, else COMMAND's status; INT, TERM and HUP
# are forwarded as TERM, exit 128 + that signal), then kill everything the run left
# behind: a descendant that called setsid (browser_service.ensure_service does)
# leaves the process group, and one that ignores TERM outlives timeout, which
# escalates only for its child.
# - This script becomes a child subreaper (through python3), so every orphan of the
#   run becomes its child, whatever its session, cgroup, environment, title or
#   dumpability. When COMMAND returns it kills the process group, then its children
#   and every process of this user whose environment carries this run's marker,
#   CASSINI_BOUNDED_RUN_<id>=1, until none is left. Without python3 only the marker
#   scan remains: a process that leaves the group and clears or rewrites its
#   environment (Chromium's title does) escapes it.
# - scope, where a systemd --user scope works: COMMAND also runs in a transient
#   scope, killed as a whole first; RuntimeMaxSec ends it even if this script is
#   killed. A process can still move itself out of the scope (Chromium asks the
#   user manager for a scope of its own when it reaches the session bus): the
#   sweep above still catches it.
# - Known limitation (D-208, accepted): the sweep is this script (python3 only
#   sets the subreaper flag, then execs it under the same pid). SIGKILL it and
#   no sweep runs. In sweep mode, without a user systemd manager, descendants
#   that called setsid have left the process group and stay alive. Scope mode,
#   the default where systemd --user works (roy), still ends everything left in
#   the scope, though only at RuntimeMaxSec (SECONDS + 2 * kill-after + 10 s); a
#   process that moved into a scope of its own (Chromium) survives in either mode.
# CASSINI_BOUNDED_RUN_MECHANISM=auto (default: scope where it works) | scope | sweep.
set -uo pipefail
whole='^0*[1-9][0-9]{0,8}$'
[ "$#" -ge 2 ] || { echo "Usage: $0 seconds command [args...]" >&2; exit 2; }
limit=$1
shift
[[ $limit =~ $whole ]] || { echo "bounded-run.sh: SECONDS must be whole seconds, from 1 to 999999999" >&2; exit 2; }
kill_after=${CASSINI_BOUNDED_RUN_KILL_AFTER:-30}
[[ $kill_after =~ $whole ]] || { echo "CASSINI_BOUNDED_RUN_KILL_AFTER must be whole seconds, from 1 to 999999999" >&2; exit 2; }

# Set (to this pid, which exec keeps) only by the re-exec below.
reaper=${_CASSINI_BOUNDED_RUN_REAPER:-}
unset _CASSINI_BOUNDED_RUN_REAPER
if [ "$reaper" != "$$" ] && [ "$reaper" != "$$-unset" ] && command -v python3 >/dev/null; then
  # PR_SET_CHILD_SUBREAPER survives exec; SIGPIPE and SIGXFSZ, which Python
  # ignores, go back to their defaults, as subprocess restores them.
  _CASSINI_BOUNDED_RUN_REAPER="$$-unset" exec python3 -I -c '
import ctypes, os, signal, sys
if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0:
    os.environ["_CASSINI_BOUNDED_RUN_REAPER"] = str(os.getpid())
for name in ("SIGPIPE", "SIGXFSZ"):
    signal.signal(getattr(signal, name), signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])
' "$BASH" "$0" "$limit" "$@"
fi
limit=$((10#$limit)) kill_after=$((10#$kill_after))

# The wrappers give the command a private XDG_RUNTIME_DIR: reach the user
# manager through its own socket, and give the command back its environment.
manager=
for dir in "${XDG_RUNTIME_DIR:-}" "/run/user/$UID"; do
  if [ -n "$dir" ] && [ -S "$dir/systemd/private" ]; then manager=$dir; break; fi
done
# --slice-inherit: the scope stays in the caller's slice and under its limits (a
# Codex agent's codex.slice caps memory), not in app.slice.
scope_works() {
  [ -n "$manager" ] && command -v systemd-run >/dev/null &&
    XDG_RUNTIME_DIR=$manager timeout 10 systemd-run --user --scope --slice-inherit --quiet --collect true >/dev/null 2>&1
}
mechanism=${CASSINI_BOUNDED_RUN_MECHANISM:-auto}
case "$mechanism" in
  auto) if scope_works; then mechanism=scope; else mechanism=sweep; fi ;;
  scope) scope_works || { echo "bounded-run.sh: no systemd --user scope here" >&2; exit 125; } ;;
  sweep) ;;
  *) echo "CASSINI_BOUNDED_RUN_MECHANISM must be auto, scope or sweep" >&2; exit 2 ;;
esac

# A variable of its own per run, so a nested run keeps the outer run's marker;
# a random UUID, so the unit name never names someone else's unit.
read -r id </proc/sys/kernel/random/uuid 2>/dev/null || id="$$-$RANDOM$RANDOM$RANDOM$RANDOM"
id=${id//-/_}
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
  XDG_RUNTIME_DIR=$manager systemd-run --user --scope --slice-inherit --quiet --collect --unit="$unit" \
    -p RuntimeMaxSec=$((limit + 2 * kill_after + 10)) -p TimeoutStopSec="$kill_after" \
    -- env "${drop[@]}" "${keep[@]}" "${bounded[@]}" &
else
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
# Reaped: its pid may name another process now, which a late signal must not reach.
run_group=$run run=''

marked() {
  local dir entries
  for dir in /proc/[0-9]*; do
    if [ "${dir#/proc/}" = "$$" ] || [ ! -O "$dir" ]; then continue; fi
    # Unreadable once the process has exited, even as a zombie.
    mapfile -t -d '' entries 2>/dev/null <"$dir/environ" || continue
    [[ " ${entries[*]} " == *" $marker=1 "* ]] && echo "${dir#/proc/}"
  done
}
if [ "$mechanism" = scope ]; then
  scope() { XDG_RUNTIME_DIR=$manager systemctl --user "$@" 2>/dev/null; }
  scope kill --signal=KILL "$unit"
  # Gone once every member has exited (--collect unloads it: "inactive"). A failing
  # systemctl proves nothing: say so; the sweep below still runs.
  for ((pass = 0; pass < 200; pass++)); do
    state=$(scope show -P ActiveState "$unit") || { echo "bounded-run.sh: could not check $unit" >&2; break; }
    case $state in active | deactivating | activating | reloading) ;; *) break ;; esac
    sleep .05
  done
  [ "$pass" != 200 ] || echo "bounded-run.sh: $unit is still $state" >&2
fi
# timeout leads its own process group (pgid = its pid).
kill -KILL -- "-$run_group" 2>/dev/null
# Each pass kills what the last one missed (a fork in between, an orphan just
# adopted) and lets bash reap it (sleep); bounded.
swept=1
for ((pass = 0; pass < 200; pass++)); do
  pids=$(marked) children=()
  if [ "$reaper" = "$$" ]; then read -r -a children <"/proc/$$/task/$$/children" || true; fi
  [ -z "$pids" ] && [ "${#children[@]}" = 0 ] && { swept=0; break; }
  # shellcheck disable=SC2086
  kill -KILL $pids "${children[@]}" 2>/dev/null
  sleep .02
done
[ "$swept" = 0 ] || echo "bounded-run.sh: processes of this run ($mechanism) are still alive" >&2
[ "$signalled" = 0 ] || status=$signalled
exit "$status"
