#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
unset WAYLAND_DISPLAY WAYLAND_SOCKET DISPLAY DBUS_SESSION_BUS_ADDRESS CASSINI_SCREENSHOT_ISOLATED CASSINI_SCREENSHOT_OUT PYTHONPATH
export GDK_BACKEND=x11 GSK_RENDERER=cairo GSETTINGS_BACKEND=memory CASSINI_ISOLATED_AUDIO=1
export PULSE_SERVER=unix:/out/no-audio.sock PIPEWIRE_REMOTE=cassini-no-audio
exec dbus-run-session -- xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' /opt/node/bin/node /source/tests/browser-setup/installed.mjs /out
