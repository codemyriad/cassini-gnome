#!/bin/bash
# Inside the harness container: optional private Pulse (PULSE=1, null sinks as tests/recording
# and R93 used), then the stall reproduction. Writes /out/stall.jsonl and /out/pulse-server.log.
set -euo pipefail
export PLAYWRIGHT_BROWSERS_PATH=/cache/playwright-protocol
if [ "${PULSE:-0}" = 1 ]; then
  runtime=$(mktemp -d /tmp/cassini-audio-XXXXXX); mkdir -m 700 "$runtime/daemon"
  cat > "$runtime/server.pa" <<PA
load-module module-native-protocol-unix socket=$runtime/pulse.sock auth-anonymous=1
load-module module-null-sink sink_name=cassini_desktop rate=48000 channels=2
load-module module-null-sink sink_name=cassini_microphone rate=48000 channels=1
set-default-sink cassini_desktop
set-default-source cassini_microphone.monitor
PA
  PULSE_RUNTIME_PATH="$runtime/daemon" HOME="$runtime/daemon" pulseaudio -n --daemonize --exit-idle-time=-1 --disable-shm=yes \
    --file="$runtime/server.pa" --log-level=info --log-time=true --log-target="file:/out/pulse-server.log"
  for _ in $(seq 1 100); do [ -S "$runtime/pulse.sock" ] && break; sleep .1; done   # bound on the socket appearing
  test -S "$runtime/pulse.sock"
  export PULSE_SERVER="unix:$runtime/pulse.sock"
fi
STALL_OUT=${STALL_OUT:-/out/stall.jsonl} node "${STALL_SCRIPT:-/source/tests/capture-render-stall.mjs}"
