#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Fresh installed deb + Flatpak, with a private headless GNOME 50 compositor.
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }
include_flatpak=1
if [[ ${1:-} == --deb-only ]]; then
  include_flatpak=0
  shift
  package=$(realpath "$1")
  bundle=""
  output=$(realpath -m "$2")
else
  package=$(realpath "$1")
  bundle=$(realpath "$2")
  output=$(realpath -m "$3")
fi
source_root=$(cd "$(dirname "$0")/../.." && pwd)
cache=${CASSINI_DESKTOP_FLATPAK_CACHE:-"$output/flatpak-cache"}
mkdir -p "$output" "$cache" "$output/home"
context=$(mktemp -d "$output/image-XXXXXXXX")
image="cassini-background-desktop:$(sha256sum "$package" | cut -c1-12)"
cleanup() {
  rm -rf "$context"
  docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cp "$package" "$context/app.deb"
"$source_root/tests/harness-image.sh" depends > "$context/depends"
# The harness stage (tests/harness-image.sh): built when its tag is new, or loaded from CI's cache.
# Its base is pinned to one digest (here, or for the whole job by CI's cache key)
# that both its tag and its build name.
export CASSINI_HARNESS_BASES
CASSINI_HARNESS_BASES=$("$source_root/tests/harness-image.sh" pin desktop)
base=$("$source_root/tests/harness-image.sh" base desktop)
harness=$("$source_root/tests/harness-image.sh" tag desktop)
if ! docker image inspect "$harness" >/dev/null 2>&1; then
  "$source_root/build-aux/ci/docker-build-bounded.sh" "desktop cached harness" docker build --pull --target harness --build-arg BASE="$base" --label cassini.scope=background-desktop-harness \
    -t "$harness" -f "$source_root/tests/desktop/Dockerfile" "$context"
  "$source_root/tests/harness-image.sh" prune desktop
fi
"$source_root/build-aux/ci/docker-build-bounded.sh" "desktop current-archive refresh" docker build --no-cache --label cassini.scope=background-desktop-test --build-arg HARNESS="$harness" \
  --build-arg TEST_UID="$(id -u)" --build-arg TEST_GID="$(id -g)" \
  -t "$image" -f "$source_root/tests/desktop/Dockerfile" "$context"
# timeout must be the container's PID 1, so that its exit ends everything left in
# the container: refuse an ENTRYPOINT inherited from the harness image.
entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$image")
[ "$entrypoint" = null ] || { echo "$image has ENTRYPOINT $entrypoint: timeout must be PID 1" >&2; exit 2; }
# Download runtime outside the offline desktop when testing Flatpak; the cache contains no test home.
if [[ $include_flatpak == 1 ]]; then
  docker run --rm --label cassini.scope=background-desktop-prepare \
    -v "$cache:/flatpak:rw" -v "$bundle:/tmp/app.flatpak:ro" -v "$source_root:/source:ro" \
    -e FLATPAK_USER_DIR=/flatpak -e FLATPAK_GL_DRIVERS=default \
    -e DBUS_SYSTEM_BUS_ADDRESS=unix:path=/tmp/cassini-qa-system-bus \
    "$image" timeout --kill-after=30 "$limit" dbus-run-session -- sh -ec '
      dbus-daemon --config-file=/source/tests/desktop/system-bus.conf --fork --nopidfile
      flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
      flatpak install --user --noninteractive flathub org.gnome.Platform//50
      flatpak install --user --noninteractive --reinstall /tmp/app.flatpak
      rm -rf /flatpak/db
    '
fi
# Flatpak creates nested unprivileged mount/user namespaces. No host devices,
# display, buses, sockets or home are mounted; all capabilities are dropped.
for indicator in 0 1; do
  scenario="$output/no-host"
  if [ "$indicator" = 1 ]; then scenario="$output/appindicator"; fi
  mkdir -p "$scenario/home"
  docker run --rm --cap-drop=ALL --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
    --network=none --label cassini.scope=background-desktop-test \
    --cpus=4 --memory=4g --shm-size=256m \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
    -v "$cache:/flatpak:rw" -v "$source_root:/source:ro" -v "$scenario:/out:rw" \
    -v "$scenario/home:/home/qa:rw" \
    -e FLATPAK_USER_DIR=/flatpak -e FLATPAK_GL_DRIVERS=default -e CASSINI_QA_DESKTOP=1 \
    -e CASSINI_QA_INCLUDE_FLATPAK="$include_flatpak" -e CASSINI_QA_INDICATOR="$indicator" \
    "$image" timeout --kill-after=30 "$limit" bash /source/tests/desktop/session.sh 2>&1 | tee "$scenario/session.log"
done
