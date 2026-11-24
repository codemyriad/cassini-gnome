#!/usr/bin/env bash
# Run a command against a PRIVATE PulseAudio server with a null sink, so audible browser tests
# (worker-recovery, bfcache, independent-sessions) never reach the desktop sound server.
#
#   tests/private-audio.sh node tests/bfcache.mjs
#   tests/private-audio.sh --fetch     # one-time: unpack the pulseaudio .deb into ~/.cache (no install, no root)
#
# The daemon gets its own runtime directory, socket and sink, loads no device modules and never
# touches the user's running audio (a PipeWire desktop keeps its own pipewire-pulse untouched).
set -euo pipefail
cache="${CASSINI_PRIVATE_AUDIO_CACHE:-$HOME/.cache/cassini-private-audio}"
if [ "${1:-}" = "--fetch" ]; then
  work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
  (cd "$work" && apt-get download pulseaudio >/dev/null && mkdir -p "$cache/root" && dpkg -x pulseaudio_*.deb "$cache/root")
  echo "unpacked pulseaudio into $cache/root"; exit 0
fi
[ "$#" -gt 0 ] || { echo "usage: $0 [--fetch | command [args...]]" >&2; exit 2; }
# CI (and an outer harness) already provides the isolated server and exports the marker: just run.
if [ "${CASSINI_ISOLATED_AUDIO:-}" = 1 ] && [ -n "${PULSE_SERVER:-}" ]; then exec "$@"; fi
command -v pactl >/dev/null || { echo "missing pactl (pulseaudio-utils)" >&2; exit 2; }
if [ -x "$cache/root/usr/bin/pulseaudio" ]; then
  daemon=("$cache/root/usr/bin/pulseaudio")
  libs="$cache/root/usr/lib/x86_64-linux-gnu/pulseaudio"
  modules=$(ls -d "$cache"/root/usr/lib/pulse-*/modules | head -1)
  libs="$libs:$modules"
elif command -v pulseaudio >/dev/null; then
  daemon=("$(command -v pulseaudio)"); libs=""; modules=""
else
  echo "no pulseaudio binary: run '$0 --fetch' once (unpacks the .deb into $cache, installs nothing)" >&2; exit 2
fi
private=$(mktemp -d /tmp/cassini-audio-XXXXXX)
chmod 700 "$private"; mkdir -p "$private/daemon"
pids=()
cleanup() { for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done; wait 2>/dev/null || true; rm -rf "$private"; }
trap cleanup EXIT
cat > "$private/server.pa" <<CONF
load-module module-native-protocol-unix socket=$private/pulse.sock auth-anonymous=1
load-module module-null-sink sink_name=cassini_desktop sink_properties=device.description=CassiniPrivateSink
set-default-sink cassini_desktop
CONF
args=(-n --daemonize=no --exit-idle-time=-1 --use-pid-file=no --disable-shm --file="$private/server.pa" --log-target="file:$private/server.log")
[ -z "$modules" ] || args+=(--dl-search-path="$modules")
# A private runtime dir and no session bus: the daemon cannot find, replace or pause the desktop server.
env DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent ${libs:+LD_LIBRARY_PATH="$libs"} PULSE_RUNTIME_PATH="$private/daemon" XDG_RUNTIME_DIR="$private/daemon" \
  PULSE_STATE_PATH="$private/daemon" PULSE_CONFIG_PATH="$private/daemon" "${daemon[@]}" "${args[@]}" >"$private/stdout.log" 2>&1 & pids+=($!)
for _ in $(seq 1 80); do [ -S "$private/pulse.sock" ] && break; sleep .1; done
[ -S "$private/pulse.sock" ] || { echo "private pulse did not start" >&2; cat "$private/stdout.log" "$private/server.log" >&2 2>/dev/null; exit 2; }
export PULSE_SERVER="unix:$private/pulse.sock" CASSINI_ISOLATED_AUDIO=1
[ "$(pactl get-default-sink)" = cassini_desktop ] || { echo "private sink is not the default" >&2; exit 2; }
"$@"
