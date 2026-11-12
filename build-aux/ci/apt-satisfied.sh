#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Exit 0 when every PKG (or PKG=VERSION, exactly that version) is installed, else 1; prints what is missing.
# For steps that would otherwise run apt-get update and install packages the runner image already holds
# (a pool host's image has the pinned screenshot fonts, gh and locales: GNOME-CI-CACHING.md item 4).
#
#   build-aux/ci/apt-satisfied.sh PKG[=VERSION]...
set -uo pipefail
missing=()
for spec in "$@"; do
  pkg=${spec%%=*}
  have=$(dpkg-query -W -f '${db:Status-Status} ${Version}' "$pkg" 2>/dev/null) || { missing+=("$spec"); continue; }
  [ "${have%% *}" = installed ] || { missing+=("$spec"); continue; }
  if [ "$spec" != "$pkg" ] && [ "${have#* }" != "${spec#*=}" ]; then missing+=("$spec (have ${have#* })"); fi
done
[ ${#missing[@]} -eq 0 ] && exit 0
echo "not installed: ${missing[*]}" >&2
exit 1
