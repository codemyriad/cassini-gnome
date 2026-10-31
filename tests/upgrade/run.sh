#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
source_root=$(cd "$(dirname "$0")/../.." && pwd)
package=$(realpath "$1")
transition=$(realpath "$2")
output=$(realpath -m "$3")
mkdir -p "$output"
context=$(mktemp -d "$output/context-XXXXXXXX")
image="cassini-talk-upgrade:$(sha256sum "$package" | cut -c1-12)"
cleanup() {
  rm -rf "$context"
  docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Test the published package, not a reconstructed approximation of 0.3.0.
gh release download v0.3.0 --repo codemyriad/cassini-gnome \
  --pattern cassini-viewer_0.3.0.ubuntu26.04_amd64.deb --dir "$context"
mv "$context/cassini-viewer_0.3.0.ubuntu26.04_amd64.deb" "$context/old.deb"
printf '%s  %s\n' 1723a58e41b4435511a57240de6a56d175a06338f7480021d9339e3bafd45a6f \
  "$context/old.deb" | sha256sum --check
# Last main before the rename: actual old Meson files, not a schema-only mock.
git -C "$source_root" archive 9cc813bbe073c4d2f0c694014b6dbe62522b8f98 > "$context/old-source.tar"
available=$(df --output=avail -B1 / | tail -n1)
if (( available < 5 * 1024 * 1024 * 1024 )); then
  echo 'Need at least 5 GiB on / before building the upgrade image' >&2
  exit 1
fi
docker build -t "$image" -f "$source_root/tests/upgrade/Dockerfile" "$context"
chmod 0777 "$output"
docker run --rm --network=none --cpus=4 --memory=3g \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m --shm-size=128m \
  -v "$source_root:/source:ro" -v "$package:/packages/new.deb:ro" \
  -v "$transition:/packages/transition.deb:ro" -v "$output:/out:rw" \
  -v "$context/old-source.tar:/old-source.tar:ro" \
  "$image" dbus-run-session -- xvfb-run -a \
  bash -ec 'python3 /source/tests/upgrade/journey.py; bash /source/tests/upgrade/source-install.sh /out/source-install /old-source.tar' \
  2>&1 | tee "$output/upgrade.log"
