#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# ROOTFS is the coordinator's QA Ubuntu root, with WebKit 6 available either
# there or in CASSINI_WEBKIT_OVERLAY. This creates exactly one private desktop.
set -euo pipefail
[[ $# -ge 2 ]] || { echo "Usage: $0 ROOTFS OUTPUT [hour|four-hour|hour-plain ...]" >&2; exit 2; }
rootfs=$(realpath "$1")
output=$(realpath -m "$2")
shift 2
source_root=$(realpath "$(dirname "$0")/../..")
state=$(mktemp -d /tmp/cassini-webkit-qa-XXXXXXXX)
heavy_lock=/mnt/ai/cassini/dev/spikes/.heavy.lock
locked=0
qa_pid=
cleanup() {
    if [[ -n $qa_pid ]]; then kill "$qa_pid" 2>/dev/null || true; wait "$qa_pid" 2>/dev/null || true; fi
    if [[ $locked == 1 ]]; then rm -f "$heavy_lock/owner"; rmdir "$heavy_lock"; fi
    # Retain this run's logs/configuration for reproducibility; never clean a
    # shared QA directory or another instance's desktop/processes.
}
trap cleanup EXIT INT TERM
echo "QA state: $state"
echo "Waiting for the shared heavy-step lock: $heavy_lock"
until mkdir "$heavy_lock" 2>/dev/null; do sleep 5; done
locked=1
printf 'webkit runner pid=%s\n' "$$" > "$heavy_lock/owner"
free -m
available=$(awk '/MemAvailable:/ { print int($2/1024) }' /proc/meminfo)
(( available > 4000 )) || { echo "RAM rule: only $available MB available" >&2; exit 1; }
mkdir -p "$output"
cat > "$state/system-bus.conf" <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>system</type><listen>unix:path=/run/dbus/system_bus_socket</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>
EOF
printf 'root:x:0:0::/root:/bin/bash\nqa:x:1000:1000:QA:/home/qa:/bin/bash\n' > "$state/passwd"
printf 'root:x:0:\nqa:x:1000:\n' > "$state/group"
cat > "$state/inner.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
dbus-daemon --config-file=/mnt/system-bus.conf --fork --nopidfile
exec dbus-run-session -- bash /mnt/desktop.sh "$@"
EOF
cat > "$state/desktop.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
gnome-shell --headless --unsafe-mode --virtual-monitor 1280x800 --no-x11 >/mnt/output/shell.log 2>&1 &
shell_pid=$!
trap 'kill "$shell_pid" 2>/dev/null || true' EXIT
export WAYLAND_DISPLAY=wayland-0
ready=0
for _ in $(seq 300); do
    if [[ -S "$XDG_RUNTIME_DIR/wayland-0" ]] && gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval 'if(Main.layoutManager._startingUp)Main.layoutManager._startupAnimationComplete();Main.overview.hide();Main.layoutManager._queueUpdateRegions();true' >/dev/null 2>&1; then ready=1; break; fi
    sleep .1
done
[[ $ready == 1 ]] || { echo "Private compositor did not become ready" >&2; exit 1; }
mkdir -p /mnt/output/build/src
cd /mnt/source
glib-compile-resources src/cassini-talk.gresource.xml --sourcedir=src --target=/mnt/output/build/src/cassini-talk.gresource
export CASSINI_WEBKIT_QA=1 CASSINI_TEST_BUILD=/mnt/output/build CASSINI_WEBKIT_OUT=/mnt/output
python3 tests/ui/transcript-webkit.py "$@"
EOF
layers=(--ro-bind "$rootfs" /)
if [[ -n ${CASSINI_WEBKIT_OVERLAY:-} ]]; then
    layers=(--overlay-src "$rootfs" --overlay-src "$(realpath "$CASSINI_WEBKIT_OVERLAY")" --ro-overlay /)
fi
bwrap "${layers[@]}" --dev /dev --proc /proc --ro-bind /sys /sys \
    --tmpfs /tmp --tmpfs /run --tmpfs /home --tmpfs /mnt \
    --dir /home/qa --dir /run/user/1000 --chmod 0700 /run/user/1000 --dir /run/dbus \
    --ro-bind "$source_root" /mnt/source --bind "$output" /mnt/output \
    --ro-bind "$state/system-bus.conf" /mnt/system-bus.conf \
    --ro-bind "$state/passwd" /etc/passwd --ro-bind "$state/group" /etc/group \
    --ro-bind "$state/inner.sh" /mnt/inner.sh --ro-bind "$state/desktop.sh" /mnt/desktop.sh \
    --unshare-all --die-with-parent --new-session --uid 1000 --gid 1000 --hostname webkit-spike \
    --setenv HOME /home/qa --setenv USER qa --setenv XDG_RUNTIME_DIR /run/user/1000 \
    --setenv XDG_SESSION_TYPE wayland --setenv XDG_CURRENT_DESKTOP GNOME \
    --setenv DBUS_SYSTEM_BUS_ADDRESS unix:path=/run/dbus/system_bus_socket \
    --setenv LANG C.UTF-8 --setenv PATH /usr/local/bin:/usr/bin:/bin \
    --setenv GSK_RENDERER cairo --setenv WEBKIT_DISABLE_COMPOSITING_MODE 1 \
    --setenv WEBKIT_DISABLE_DMABUF_RENDERER 1 \
    bash /mnt/inner.sh "$@" > "$output/run.log" 2>&1 &
qa_pid=$!
wait "$qa_pid"
qa_pid=
