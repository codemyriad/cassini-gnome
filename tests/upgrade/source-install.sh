#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Two actual Meson installations into one prefix, without uninstalling the old.
set -euo pipefail
task_root=$(realpath -m "$1")
old_archive=$(realpath "$2")
current_source=$(realpath "${3:-$(dirname "$0")/../..}")
mkdir -p "$task_root/old-source" "$task_root/config"
tar -xf "$old_archive" -C "$task_root/old-source"
prefix="$task_root/prefix"
meson setup "$task_root/old-build" "$task_root/old-source" --prefix="$prefix" \
  -Dprofile=development -Dwhistle=disabled -Dsherpa=disabled
meson install -C "$task_root/old-build"
export GSETTINGS_SCHEMA_DIR="$prefix/share/glib-2.0/schemas"
export GSETTINGS_BACKEND=keyfile XDG_CONFIG_HOME="$task_root/config"
gsettings set com.gocassini.Viewer window-width 935
meson setup "$task_root/new-build" "$current_source" --prefix="$prefix" \
  -Dprofile=development -Dwhistle=disabled -Dsherpa=disabled
meson install -C "$task_root/new-build"
glib-compile-schemas --strict "$GSETTINGS_SCHEMA_DIR"
PYTHONPATH="$prefix/share/cassini-viewer" python3 - <<'PY'
from cassini_viewer.identity import settings
assert settings().get_int('window-width') == 935
assert settings().get_boolean('legacy-settings-migrated')
PY
"$prefix/bin/cassini-talk" --help >/dev/null
printf 'PASS consecutive Meson installs: both schemas, migrated preference and launcher\n'
