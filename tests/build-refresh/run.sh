#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
package=$(realpath "$1")
output=$(realpath -m "$2")
source_root=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$output"
context=$(mktemp -d "$output/image-XXXXXXXX")
private_home=$(mktemp -d "$output/home-XXXXXXXX")
image="cassini-build-refresh:$(sha256sum "$package" | cut -c1-12)"
cleanup() {
 rm -rf "$context" "$private_home"
 docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cp "$package" "$context/app.deb"
docker build --label cassini.scope=installed-build-refresh \
 --build-arg TEST_UID="$(id -u)" --build-arg TEST_GID="$(id -g)" \
 -t "$image" -f "$source_root/tests/build-refresh/Dockerfile" "$context"
# The installed app and both package upgrades share only this private display,
# bus, synthetic audio, and generated home. Never mount a host desktop/socket.
docker run --rm --label cassini.scope=installed-build-refresh --network=none \
 --cpus=4 --memory=3g --shm-size=128m \
 --tmpfs /tmp:rw,exec,nosuid,nodev,size=2g \
 -v "$source_root:/source:ro" -v "$package:/original.deb:ro" -v "$output:/out:rw" \
 -v "$private_home:/home/qa:rw" -e CASSINI_QA_UPGRADE=1 \
 "$image" bash /source/tests/build-refresh/session.sh 2>&1 | tee "$output/session.log"
