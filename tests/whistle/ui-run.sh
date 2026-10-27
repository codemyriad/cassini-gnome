#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
glib-compile-resources --sourcedir=src --target=/out/pipeline-ui.gresource src/cassini-viewer.gresource.xml
export HOME=/tmp XDG_RUNTIME_DIR=/tmp/cassini-ui-run XDG_CONFIG_HOME=/tmp/config XDG_DATA_HOME=/tmp/data XDG_CACHE_HOME=/tmp/cache
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export GSETTINGS_BACKEND=memory GSK_RENDERER=cairo GDK_BACKEND=x11
export CASSINI_SCREENSHOT_ISOLATED=1 ADW_DISABLE_PORTAL=1 GIO_USE_VFS=local GTK_USE_PORTAL=0
export PULSE_SERVER=unix:/tmp/no-audio PIPEWIRE_REMOTE=cassini-no-audio
unset WAYLAND_DISPLAY WAYLAND_SOCKET DISPLAY DBUS_SESSION_BUS_ADDRESS
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' \
  /usr/bin/python3 tests/whistle/ui_probe.py /bundle /out/pipeline-ui.json --resource /out/pipeline-ui.gresource
