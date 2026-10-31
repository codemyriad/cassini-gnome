#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Fresh installed deb + Flatpak, with a private headless GNOME 50 compositor.
set -euo pipefail
package=$(realpath "$1")
bundle=$(realpath "$2")
output=$(realpath -m "$3")
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
docker build --label cassini.scope=background-desktop-test \
  --build-arg TEST_UID="$(id -u)" --build-arg TEST_GID="$(id -g)" \
  -t "$image" -f "$source_root/tests/desktop/Dockerfile" "$context"
# Download runtime outside the offline desktop; the cache contains no test home.
docker run --rm --label cassini.scope=background-desktop-prepare \
  -v "$cache:/flatpak:rw" -v "$bundle:/tmp/app.flatpak:ro" -v "$source_root:/source:ro" \
  -e FLATPAK_USER_DIR=/flatpak -e FLATPAK_GL_DRIVERS=default \
  -e DBUS_SYSTEM_BUS_ADDRESS=unix:path=/tmp/cassini-qa-system-bus \
  "$image" dbus-run-session -- sh -ec '
    dbus-daemon --config-file=/source/tests/desktop/system-bus.conf --fork --nopidfile
    flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
    flatpak install --user --noninteractive flathub org.gnome.Platform//50
    flatpak install --user --noninteractive --reinstall /tmp/app.flatpak
    rm -rf /flatpak/db
  '
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
    -e CASSINI_QA_INDICATOR="$indicator" \
    "$image" bash /source/tests/desktop/session.sh 2>&1 | tee "$scenario/session.log"
done
