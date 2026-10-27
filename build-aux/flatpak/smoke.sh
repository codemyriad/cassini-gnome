#!/bin/sh
# Install a Flatpak bundle for the current user and smoke-test it the way the
# package scripts test theirs: the startup check, every GStreamer element, and
# (on x86_64) writing the D11 goldens down with the app's own worker, offline:
# the sandbox gets --unshare=network, so it has nothing but loopback.
#   build-aux/flatpak/smoke.sh BUNDLE.flatpak APP_ID
set -eu
cd "$(dirname "$0")/../.."
BUNDLE=$1 APP_ID=$2
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y --noninteractive --reinstall --bundle "$BUNDLE"
set -- -I "$PWD/build-aux/smoke-installed.py" /app/share/cassini-viewer
if [ "$(uname -m)" = x86_64 ]; then
  set -- "$@" --whistle "$PWD/tests/whistle"
fi
flatpak run --unshare=network --no-documents-portal \
  --filesystem="$PWD/build-aux:ro" --filesystem="$PWD/tests/whistle:ro" \
  --command=python3 "$APP_ID" "$@"
