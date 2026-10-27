#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: build-aux/vendor-browser-protocol.sh UPSTREAM_CHECKOUT FULL_COMMIT
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
[ "$#" -eq 2 ] || { echo "Usage: $0 upstream-checkout full-commit" >&2; exit 1; }
python3 "$1/vendor.py" "$2" "$root/browser-protocol" "$root/browser-extension"
python3 "$root/browser-protocol/verify.py" --pinned "$root/browser-extension"
