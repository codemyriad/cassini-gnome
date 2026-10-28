#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cache=${CASSINI_SHERPA_CACHE:-"$root/.sherpa-cache"}
exec python3 "$root/sherpa/fetch.py" --cache "$cache"
