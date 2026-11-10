#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Network preparation has its own budget; never retry package configuration or tests.
set -euo pipefail
[ "$#" -gt 0 ] || { echo "Usage: apt-install-bounded.sh PACKAGE..." >&2; exit 2; }
options=(-o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any)
for attempt in 1 2; do
  if timeout --kill-after=10 60 apt-get "${options[@]}" update &&
     timeout --kill-after=10 180 apt-get "${options[@]}" --download-only install -y "$@"; then
    # No network and no retry here: package/configuration failures stay failures.
    exec apt-get --no-download install -y "$@"
  fi
  if [ "$attempt" = 2 ]; then
    echo "Dependency network preparation failed after 2 bounded attempts; tests were not run." >&2
    exit 1
  fi
  echo "Dependency download failed or timed out; network retry 2/2, retaining cached files and replacing the Azure Ubuntu mirror with the primary archive." >&2
  python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ.get('CASSINI_APT_SOURCES_DIR', '/etc/apt'))
# GitHub's Ubuntu images use mirror+file: references to these mirror lists.
paths = [root / 'sources.list', *(root / 'sources.list.d').glob('*.list'), *(root / 'sources.list.d').glob('*.sources'), *root.glob('apt-mirrors*.txt')]
for path in paths:
    if path.is_file():
        before = path.read_text()
        after = before.replace('://azure.archive.ubuntu.com/ubuntu', '://archive.ubuntu.com/ubuntu')
        if after != before:
            path.write_text(after)
            print('Network retry: primary Ubuntu archive in ' + str(path), flush=True)
PY
done
