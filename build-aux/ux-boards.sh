#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Render production Library widgets against the shared, generated read-only fixtures.
set -euo pipefail
app=$(realpath "$1")
out=$(realpath -m "$2")
fixture=$(mktemp -d /tmp/cassini-library-boards-XXXXXXXX)
trap 'rm -rf "$fixture"' EXIT
python3 ux/library-vectors/make.py "$fixture"
mkdir -p "$out"
for board in library first-run; do
  case_name=main
  [ "$board" != first-run ] || case_name=empty
  CASSINI_LIBRARY_DIR="$fixture/$case_name/root" \
  CASSINI_SCREENSHOT_ACTIVITY="$fixture/$case_name/activity.json" \
  CASSINI_SCREENSHOT_NOW=1793107080000 CASSINI_SCREENSHOT_BOARD="$board" \
    build-aux/ux-screenshot.sh "$app" "$out/$board.png"
done
