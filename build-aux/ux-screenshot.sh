#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: ux-screenshot.sh INSTALLED_APP OUTPUT.png
set -euo pipefail
[ "$#" -eq 2 ] || { echo "Usage: $0 installed-app output.png" >&2; exit 1; }
app=$(realpath "$1")
output=$(realpath -m "$2")
runtime=$(mktemp -d /tmp/cassini-ux-shot-XXXXXXXX)
trap 'rm -rf "$runtime"' EXIT
mkdir -m 700 "$runtime/run" "$runtime/home"
export HOME="$runtime/home" XDG_RUNTIME_DIR="$runtime/run"
export XDG_CONFIG_HOME="$runtime/config" XDG_CACHE_HOME="$runtime/cache" XDG_DATA_HOME="$runtime/data"
export GSK_RENDERER=cairo GDK_BACKEND=x11 GSETTINGS_BACKEND=memory
export ADW_DISABLE_PORTAL=1 ADW_DEBUG_ACCENT_COLOR=blue
export TZ=Europe/Rome LANG=en_GB.UTF-8 LC_ALL=en_GB.UTF-8 LANGUAGE=en_GB
export PULSE_SERVER="unix:$runtime/no-audio.sock" PIPEWIRE_REMOTE=cassini-ux-no-audio
fonts=${CASSINI_SCREENSHOT_FONT_DIR:-/usr/share/fonts/opentype}
[ -f "$fonts/cantarell/Cantarell-VF.otf" ] || { echo "Install pinned Cantarell or set CASSINI_SCREENSHOT_FONT_DIR" >&2; exit 1; }
python3 - "$runtime/fonts.conf" "$fonts" "$runtime/font-cache" <<'PYFONT'
import html,sys
from pathlib import Path
output,root,cache=map(Path,sys.argv[1:])
output.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig>'
  + ''.join('<dir>'+html.escape(str(root/name))+'</dir>' for name in ['cantarell','inter'] if (root/name).is_dir())
  + '<cachedir>'+html.escape(str(cache))+'</cachedir>'
  + '<alias><family>sans-serif</family><prefer><family>Cantarell</family></prefer></alias></fontconfig>')
PYFONT
export FONTCONFIG_FILE="$runtime/fonts.conf"
fc-match -f '%{family}: %{file}\n' Cantarell
export CASSINI_SCREENSHOT_ISOLATED=1 CASSINI_SCREENSHOT_OUT="$output"
unset WAYLAND_DISPLAY WAYLAND_SOCKET DISPLAY DBUS_SESSION_BUS_ADDRESS
exec_args=("$app")
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' "${exec_args[@]}"
