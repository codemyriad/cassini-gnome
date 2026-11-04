#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }
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
"$source_root/tests/harness-image.sh" depends > "$context/depends"
# The harness stage (tests/harness-image.sh): built when its tag is new, or loaded from CI's cache.
harness=$("$source_root/tests/harness-image.sh" tag build-refresh)
if ! docker image inspect "$harness" >/dev/null 2>&1; then
 docker build --pull --target harness --label cassini.scope=installed-build-refresh-harness \
  -t "$harness" -f "$source_root/tests/build-refresh/Dockerfile" "$context"
 "$source_root/tests/harness-image.sh" prune build-refresh
fi
docker build --no-cache --label cassini.scope=installed-build-refresh --build-arg HARNESS="$harness" \
 --build-arg TEST_UID="$(id -u)" --build-arg TEST_GID="$(id -g)" \
 -t "$image" -f "$source_root/tests/build-refresh/Dockerfile" "$context"
# timeout must be the container's PID 1, so that its exit ends everything left in
# the container: refuse an ENTRYPOINT inherited from the harness image.
entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$image")
[ "$entrypoint" = null ] || { echo "$image has ENTRYPOINT $entrypoint: timeout must be PID 1" >&2; exit 2; }
# The installed app and both package upgrades share only this private display,
# bus, synthetic audio, and generated home. Never mount a host desktop/socket.
docker run --rm --label cassini.scope=installed-build-refresh --network=none \
 --cpus=4 --memory=3g --shm-size=128m \
 --tmpfs /tmp:rw,exec,nosuid,nodev,size=2g \
 -v "$source_root:/source:ro" -v "$package:/original.deb:ro" -v "$output:/out:rw" \
 -v "$private_home:/home/qa:rw" -e CASSINI_QA_UPGRADE=1 \
 "$image" timeout --kill-after=30 "$limit" bash /source/tests/build-refresh/session.sh 2>&1 | tee "$output/session.log"
