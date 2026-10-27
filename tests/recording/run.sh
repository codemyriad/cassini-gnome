#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
cd "$(dirname "$0")/../.."
for tool in docker xvfb-run xauth xdotool dbus-run-session; do
    command -v "$tool" >/dev/null || { echo "Missing $tool: install the recording harness prerequisites in tests/recording/README.md" >&2; exit 1; }
done
if [[ " $* " == *" tests/recording/gtk.py "* ]]; then
    command -v import >/dev/null || { echo "Missing ImageMagick import: install imagemagick for GTK screenshots" >&2; exit 1; }
fi
[ "$#" -gt 0 ] || { echo "Usage: $0 command [args...]" >&2; exit 1; }
# Resolve mise's interpreter before giving the child private XDG directories.
if [ "$1" = node ]; then
    interpreter=$(node -p 'process.execPath')
    shift
    set -- "$interpreter" "$@"
fi
artifact_root="$PWD/tests/recording/artifacts"
mkdir -p "$artifact_root"
export CASSINI_TEST_ARTIFACTS=$(mktemp -d "$artifact_root/run-XXXXXXXX")
ln -sfn "$(basename "$CASSINI_TEST_ARTIFACTS")" "$artifact_root/latest"
echo "Recording evidence: $CASSINI_TEST_ARTIFACTS"
export LANGUAGE=C
runtime=$(mktemp -d /tmp/cassini-audio-XXXXXX)
container="cassini-audio-$(id -u)-$$"
cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf "$runtime"
}
trap cleanup EXIT
cat > "$runtime/server.pa" <<'PA'
load-module module-native-protocol-unix socket=/audio/pulse.sock auth-anonymous=1
load-module module-null-sink sink_name=cassini_desktop rate=48000 channels=2
load-module module-null-sink sink_name=cassini_microphone rate=48000 channels=1
set-default-sink cassini_desktop
set-default-source cassini_microphone.monitor
PA
docker build -q -t cassini-recording-test-audio -f tests/recording/Audio.Dockerfile tests/recording
docker run -d --name "$container" --network=none --user "$(id -u):$(id -g)" \
    -v "$runtime:/audio" cassini-recording-test-audio > /dev/null
for _ in $(seq 1 100); do
    [ -S "$runtime/pulse.sock" ] && break
    sleep .1
done
if [ ! -S "$runtime/pulse.sock" ]; then docker logs "$container"; exit 1; fi
# The server's socket is private to this run; Chromium and GStreamer inherit it.
export PULSE_SERVER="unix:$runtime/pulse.sock"
unset WAYLAND_DISPLAY WAYLAND_SOCKET DBUS_SESSION_BUS_ADDRESS
mkdir -m 700 "$runtime/session"
export XDG_RUNTIME_DIR="$runtime/session"
export XDG_SESSION_TYPE=x11
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
export XDG_CONFIG_HOME="$runtime/config" XDG_CACHE_HOME="$runtime/cache" XDG_DATA_HOME="$runtime/data"
export GSETTINGS_BACKEND=memory
export GDK_BACKEND=x11
export CASSINI_ISOLATED_AUDIO=1
# Keyboard injection is only permitted inside this nested, private X server.
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' "$@"
