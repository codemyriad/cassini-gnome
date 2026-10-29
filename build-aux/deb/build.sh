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
# dpkg-buildpackage names the package after debian/changelog. The package
# carries the x86_64 writing-down engine, so it is amd64 only.
DEB=cassini-viewer_${DEB_VERSION}_amd64.deb
# Each base gets its own file name in dist/ and in the release:
# ubuntu:26.04 -> cassini-viewer_0.3.0~ubuntu26.04_amd64.deb,
# debian:testing -> cassini-viewer_0.3.0~debian-testing_amd64.deb.
BASE_TAG=$(echo "${BASE##*/}" | sed 's/:\([0-9]\)/\1/; s/[:@]/-/g')
OUT=cassini-viewer_${DEB_VERSION}~${BASE_TAG}_amd64.deb
TAG=cassini-viewer-deb:$(image_slug "$BASE")
# The pinned writing-down artifacts; the package build itself is offline.
build-aux/whistle-cache.sh
build-aux/sherpa-cache.sh
container_build -q -f build-aux/deb/Dockerfile --build-arg BASE="$BASE" -t "$TAG" debian
mkdir -p dist
container_run -v "$PWD:/in:ro$Z" -v "$PWD/dist:/out:rw$Z" -e DEB="$DEB" -e OUT="$OUT" "$TAG" sh -ec '
  mkdir -p /build/src && cd /build/src
  # Copy the tree without build output, caches and VCS data.
  tar -C /in -cf - --exclude=./.git --exclude=./.venv --exclude="./_build*" --exclude=./_install \
    --exclude=./_flatpak --exclude=./.flatpak-builder --exclude=./repo \
    --exclude=./dist --exclude="./obj-*" --exclude=./debian/.debhelper \
    --exclude=./debian/cassini-viewer --exclude=./tests/recording/node_modules \
    --exclude=./tests/recording/artifacts --exclude=./.pytest_cache --exclude=./.ruff_cache \
    --exclude=__pycache__ . | tar -xf -
  dpkg-buildpackage -us -uc -b
  test -f "../$DEB"
  lintian --info --fail-on error "../$DEB"
  apt-get update -qq
  apt-get install -y --no-install-recommends "../$DEB"
  cassini-viewer --help >/dev/null
  python3 -I /in/build-aux/smoke-installed.py
  python3 -I /in/build-aux/ci/validate_installed.py "$1"
  cp "../$DEB" "/out/$OUT"
  # Docker writes as root; hand the file back to the owner of dist/.
  # Rootless podman already maps root to the caller, so this is a no-op there.
  chown "$(stat -c %u:%g /out)" "/out/$OUT" 2>/dev/null || true
' sh "$VERSION"

# The build container has every Build-Depends installed, which can hide a
# missing Depends; install the package in a fresh container, then check it
# there with no network, writing the D11 goldens down offline.
container_offline_smoke "$BASE" '
  printf "Acquire::Retries \"3\";\nAcquire::http::Timeout \"15\";\n" > /etc/apt/apt.conf.d/80retries
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "/d/$OUT" >/dev/null
  rm -rf /var/lib/apt/lists/*
' -v "$PWD/dist:/d:ro$Z" -e OUT="$OUT" -e DEBIAN_FRONTEND=noninteractive
ls -l "dist/$OUT"
