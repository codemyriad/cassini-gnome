#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Run a separate browser profile; no extension installation or browser popup needed.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
profile=${CASSINI_BROWSER_PROFILE:-$HOME/cassini-ext-test/profile}
recordings=${CASSINI_BROWSER_RECORDINGS:-$HOME/cassini-ext-test/recordings}
python3 "$root/build-aux/prepare-browser-profile.py" --profile "$profile" --recordings "$recordings"
if [ -n "${CASSINI_CHROMIUM:-}" ]; then
    browser=$CASSINI_CHROMIUM
else
    browser=$(python3 - <<'PY'
import os,re
from pathlib import Path
root=Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH',Path.home()/'.cache/ms-playwright'))
files=[p for p in root.glob('chromium-*/chrome-linux*/chrome') if p.is_file()]
files.sort(key=lambda p:int(re.search(r'chromium-(\d+)',str(p)).group(1)))
if files: print(files[-1])
PY
)
fi
[ -x "$browser" ] || { echo "Install the test browser: npm ci --prefix tests/recording && tests/recording/node_modules/.bin/playwright install chromium" >&2; exit 1; }
echo "Open Cassini to see and record meetings. Restart this test browser after extension updates."
exec "$browser" --user-data-dir="$profile" --no-first-run --no-default-browser-check \
    --load-extension="$root/browser-extension" --disable-extensions-except="$root/browser-extension" \
    --ozone-platform-hint=auto "$@"
