#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
unset WAYLAND_DISPLAY WAYLAND_SOCKET DISPLAY DBUS_SESSION_BUS_ADDRESS
export XDG_RUNTIME_DIR=/tmp/cassini-build-refresh-runtime
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share" XDG_CACHE_HOME="$HOME/.cache"
export GDK_BACKEND=x11 GSK_RENDERER=cairo LANG=C.UTF-8
mkdir -m 700 "$XDG_RUNTIME_DIR"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" /out/bin /tmp/cassini-build-refresh-audio
# Keep the short synthetic capture independent of the host scratch disk's
# fsync latency while dpkg writes its upgrade. No host audio or files live here.
ln -s /tmp/cassini-build-refresh-audio "$HOME/Music"
# Inventory only; no browser is launched in this application-lifetime test.
printf '#!/bin/sh\nexit 0\n' > /out/bin/google-chrome
chmod 0700 /out/bin/google-chrome
export PATH="/out/bin:$PATH"
exec dbus-run-session -- xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' \
    python3 /source/tests/build-refresh/journey.py "${1:-/usr/bin/cassini-talk}" "${2:-/usr/share/cassini-talk}"
