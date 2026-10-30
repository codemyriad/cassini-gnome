#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
[ "${CASSINI_QA_DESKTOP:-}" = 1 ] || exit 1
unset DISPLAY WAYLAND_DISPLAY WAYLAND_SOCKET DBUS_SESSION_BUS_ADDRESS
export XDG_RUNTIME_DIR=/tmp/cassini-qa-runtime
mkdir -m 700 "$XDG_RUNTIME_DIR"
mkdir -p "$HOME/Music"
export XDG_DATA_DIRS=/flatpak/exports/share:/usr/local/share:/usr/share
export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/tmp/cassini-qa-system-bus
export LANG=C.UTF-8
xdg-user-dirs-update
dbus-daemon --config-file=/source/tests/desktop/system-bus.conf --fork --nopidfile
exec dbus-run-session -- bash -ec '
  gnome-shell --headless --unsafe-mode --virtual-monitor 1280x800 --no-x11 >/out/shell.log 2>&1 &
  python3 /source/tests/desktop/windowless.py
'
