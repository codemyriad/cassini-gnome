#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
package=$(realpath "$1")
output=$(realpath -m "$2")
source_root=$(cd "$(dirname "$0")/../.." && pwd)
node_binary=$(realpath "$(command -v node)")
mkdir -p "$output"
package_context=$(mktemp -d "$output/package-context-XXXXXXXX")
package_test_image="cassini-browser-package-test:$(sha256sum "$package" | cut -c1-12)"
cleanup() {
  rm -rf "$package_context"
  docker image rm "$package_test_image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cp "$package" "$package_context/app.deb"
browser_binary=$(node -e 'console.log(require(process.argv[1]).chromium.executablePath())' "$source_root/tests/recording/node_modules/playwright")
browser_cache=$(dirname "$(dirname "$(dirname "$browser_binary")")")
test -x "$browser_binary"
docker build --label cassini.scope=browser-startup-test --build-arg BASE="${CASSINI_BROWSER_TEST_BASE:-ubuntu:26.04}" \
  --build-arg TEST_UID="$(id -u)" --build-arg TEST_GID="$(id -g)" \
  -f "$source_root/tests/browser-setup/Dockerfile" -t "$package_test_image" "$package_context"
docker run --rm --label cassini.scope=browser-startup-test --network=none --cap-drop=ALL \
  --security-opt=no-new-privileges --read-only --cpus=4 --memory=3g \
  --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m --shm-size=128m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PLAYWRIGHT_BROWSERS_PATH=/browsers \
  -v "$source_root:/source:ro" -v "$output:/out:rw" -v "$browser_cache:/browsers:ro" \
  -v "$node_binary:/opt/node/bin/node:ro" -w /source "$package_test_image" \
  bash /source/tests/browser-setup/session.sh 2>&1 | tee "$output/session.log"
