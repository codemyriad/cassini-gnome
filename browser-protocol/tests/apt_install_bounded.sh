# SPDX-License-Identifier: GPL-3.0-or-later
# Network preparation has its own budget; never retry package configuration or tests.
set -euo pipefail

has_pkg=0
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *) has_pkg=1 ;;
  esac
done
[ "$has_pkg" -eq 1 ] || { echo "Usage: apt_install_bounded.sh [APT_FLAG...] PACKAGE..." >&2; exit 2; }

apt_root="${CASSINI_APT_SOURCES_DIR:-/etc/apt}"
mkdir -p "$apt_root/apt.conf.d"
cat > "$apt_root/apt.conf.d/80cassini-network" <<'EOF'
Acquire::Retries "3";
Acquire::Retries::Delay "true";
Acquire::Retries::Delay::Maximum "5";
Acquire::http::Timeout "15";
Acquire::https::Timeout "15";
APT::Update::Error-Mode "any";
EOF

options=(-o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any)
for attempt in 1 2 3; do
  if timeout --kill-after=10 60 apt-get "${options[@]}" update &&
     timeout --kill-after=10 180 apt-get "${options[@]}" --download-only install -y "$@"; then
    # No network and no retry here: package/configuration failures stay failures.
    exec apt-get --no-download install -y "$@"
  fi
  if [ "$attempt" = 3 ]; then
    echo "Dependency network preparation failed after 3 bounded attempts; tests were not run." >&2
    exit 1
  fi
  if [ "$attempt" = 1 ]; then
    echo "Dependency download failed or timed out; network retry 2/3, replacing Azure Ubuntu mirror with primary archive." >&2
  else
    echo "Dependency download failed or timed out; network retry 3/3, replacing Ubuntu archive/security mirrors with mirrors.edge.kernel.org over HTTPS." >&2
  fi
  APT_RETRY_ATTEMPT="$attempt" python3 - <<'PY'
import os
import re
from pathlib import Path

root = Path(os.environ.get('CASSINI_APT_SOURCES_DIR', '/etc/apt'))
attempt = int(os.environ['APT_RETRY_ATTEMPT'])
paths = [
    root / 'sources.list',
    *(root / 'sources.list.d').glob('*.list'),
    *(root / 'sources.list.d').glob('*.sources'),
    *root.glob('apt-mirrors*.txt'),
]
for path in paths:
    if not path.is_file():
        continue
    before = path.read_text(encoding='utf-8')
    if attempt == 1:
        after = before.replace('://azure.archive.ubuntu.com/ubuntu', '://archive.ubuntu.com/ubuntu')
    else:
        after = re.sub(
            r'https?://(?:(?:azure\.)?archive|security)\.ubuntu\.com/ubuntu/?',
            'https://mirrors.edge.kernel.org/ubuntu/',
            before,
        )
        if path.name.startswith('apt-mirrors'):
            seen = []
            for line in after.splitlines():
                if line not in seen:
                    seen.append(line)
            after = '\n'.join(seen) + ('\n' if before.endswith('\n') else '')
    if after != before:
        path.write_text(after, encoding='utf-8')
        label = 'primary Ubuntu archive' if attempt == 1 else 'mirrors.edge.kernel.org HTTPS mirror'
        print(f'Network retry: {label} in {path}', flush=True)
PY
done
