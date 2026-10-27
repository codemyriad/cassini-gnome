#!/bin/sh
# Build the Debian package in a container, install it there and smoke-test it,
# then copy it to ./dist. Uses podman or docker (see build-aux/container.sh).
#   build-aux/deb/build.sh [base-image]      (default: ubuntu:26.04)
# Base images known to work: ubuntu:26.04, ubuntu:25.10, debian:testing,
# debian:sid.
# The package needs GTK >= 4.18 and libadwaita >= 1.8, so older releases
# (ubuntu:24.04, debian:13) cannot install it.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
BASE=${1:-ubuntu:26.04}
VERSION=$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" meson.build | head -n1)
DEB_VERSION=$(sed -n '1s/^[^ ]* (\([^)]*\)).*/\1/p' debian/changelog)
if [ "$VERSION" != "${DEB_VERSION%%-*}" ]; then
  echo "error: meson.build says $VERSION but debian/changelog says $DEB_VERSION" >&2
  exit 1
fi
TAG=cassini-viewer-deb:$(image_slug "$BASE")
container_build -q -f build-aux/deb/Dockerfile --build-arg BASE="$BASE" -t "$TAG" debian
mkdir -p dist
container_run -v "$PWD:/in:ro$Z" -v "$PWD/dist:/out:rw$Z" "$TAG" sh -ec '
  mkdir -p /build/src && cd /build/src
  # Copy the tree without build output, caches and VCS data.
  tar -C /in -cf - --exclude=./.git --exclude=./.venv --exclude="./_build*" --exclude=./_install \
    --exclude=./_flatpak --exclude=./.flatpak-builder --exclude=./repo \
    --exclude=./dist --exclude="./obj-*" --exclude=./debian/.debhelper \
    --exclude=./debian/cassini-viewer --exclude=./tests/recording/node_modules \
    --exclude=./tests/recording/artifacts --exclude=./.pytest_cache --exclude=./.ruff_cache \
    --exclude=__pycache__ . | tar -xf -
  dpkg-buildpackage -us -uc -b
  lintian --info --fail-on error ../*.deb
  apt-get update -qq
  apt-get install -y --no-install-recommends ../*.deb
  # The same startup check the launcher runs: Python, GTK, libadwaita, GStreamer.
  python3 -c "import sys; sys.path.insert(0, \"/usr/share/cassini-viewer\")
from cassini_viewer import runtime
problems = runtime._problems()
if not problems:
    from gi.repository import Gst
    problems = runtime.missing_gst_elements(Gst.ElementFactory.find)
sys.exit(\"smoke test failed: %s\" % problems if problems else 0)"
  cassini-viewer --help >/dev/null
  echo "smoke test passed"
  cp ../*.deb /out/
  # Docker writes as root; hand the file back to the owner of dist/.
  # Rootless podman already maps root to the caller, so this is a no-op there.
  chown "$(stat -c %u:%g /out)" /out/*.deb 2>/dev/null || true
'
ls -l dist/*.deb
