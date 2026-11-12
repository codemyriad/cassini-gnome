#!/bin/sh
# Build the Debian package in a container, install it there and smoke-test it,
# then copy it to ./dist. Uses podman or docker (see build-aux/container.sh).
#   build-aux/deb/build.sh [--nocheck] [base-image]      (default: ubuntu:26.04)
# Base images known to work: ubuntu:26.04, ubuntu:25.10, debian:testing,
# debian:sid.
# The package needs GTK >= 4.18 and libadwaita >= 1.8, so older releases
# (ubuntu:24.04, debian:13) cannot install it.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
NOCHECK=0
if [ "${1:-}" = "--nocheck" ]; then
  NOCHECK=1
  shift
fi
case "$#:${1:-}" in
  0:|1:[!-]*) ;;
  *) echo "usage: $0 [--nocheck] [base-image]" >&2; exit 2 ;;
esac
BASE=${1:-ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96}
VERSION=$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" meson.build | head -n1)
DEB_VERSION=$(sed -n '1s/^[^ ]* (\([^)]*\)).*/\1/p' debian/changelog)
if [ "$VERSION" != "${DEB_VERSION%%[+-]*}" ]; then
  echo "error: meson.build says $VERSION but debian/changelog says $DEB_VERSION" >&2
  exit 1
fi
# dpkg-buildpackage names the package after debian/changelog. The package
# carries the x86_64 writing-down engine, so it is amd64 only.
DEB=cassini-talk_${DEB_VERSION}_amd64.deb
# Each base gets its own file name in dist/ and in the release:
# ubuntu:26.04 -> cassini-talk_0.4.0~ubuntu26.04_amd64.deb,
# debian:testing -> cassini-talk_0.4.0~debian-testing_amd64.deb.
BASE_NO_DIGEST=${BASE%@sha256:*}
BASE_TAG=$(echo "${BASE_NO_DIGEST##*/}" | sed 's/:\([0-9]\)/\1/; s/[:@]/-/g')
OUT=cassini-talk_${DEB_VERSION}~${BASE_TAG}_amd64.deb
TAG=cassini-talk-deb:$(image_slug "$BASE")
# The pinned writing-down artifacts; the package build itself is offline.
build-aux/whistle-cache.sh
build-aux/sherpa-cache.sh
# Use the @sha256-pinned prebuilt GHCR image when debian/control + Dockerfile
# match build-aux/ci/image-pins.json; otherwise build locally from $BASE so a PR
# that modifies Build-Depends validates its change immediately.
BUILD_IMAGE=$(python3 build-aux/ci/ci_images.py pull deb "$BASE" || true)
if [ -z "$BUILD_IMAGE" ]; then
  container_pull "$BASE"
  # The image only installs the build dependencies (no build, no test), so a
  # mirror failure while building it is retried as a whole (retry.sh).
  # Keep dependency-install output: quiet mode hides apt/mk-build-deps failures,
  # leaving only a Dockerfile line and an exit code in CI.
  retry container_build -f build-aux/deb/Dockerfile --build-arg BASE="$BASE" -t "$TAG" debian
  BUILD_IMAGE=$TAG
fi
mkdir -p dist
logs=$(test_log_dir "deb-$(image_slug "$BASE")")
pkg_cache_init
# shellcheck disable=SC2086 # PKG_CACHE_MOUNTS is a list of run options
container_run $PKG_CACHE_MOUNTS -v "$PWD:/in:ro$Z" -v "$PWD/dist:/out:rw$Z" ${logs:+-v "$logs:/logs:rw$Z"} -e DEB="$DEB" -e OUT="$OUT" -e NOCHECK="$NOCHECK" "$BUILD_IMAGE" sh -ec '
  . /in/build-aux/ci/retry.sh
  # Keep the Meson test logs, also when a test fails (test_log_dir).
  save_logs() {
    [ -d /logs ] || return 0
    for d in /build/src/obj-*/meson-logs; do [ ! -d "$d" ] || cp -a "$d/." /logs/; done
    cp /etc/os-release /logs/os-release  # G11: the image that ran the tests
    chown -R "$(stat -c %u:%g /logs)" /logs
  }
  trap "save_logs || true" EXIT
  mkdir -p /build/src && cd /build/src
  # Copy the tree without build output, caches and VCS data.
  tar -C /in -cf - --exclude=./.git --exclude=./.venv --exclude="./_build*" --exclude=./_install \
    --exclude=./_flatpak --exclude=./.flatpak-builder --exclude=./repo \
    --exclude=./dist --exclude="./obj-*" --exclude=./debian/.debhelper \
    --exclude=./debian/cassini-talk --exclude=./debian/tmp --exclude=./tests/recording/node_modules \
    --exclude=./tests/recording/artifacts --exclude=./.pytest_cache --exclude=./.ruff_cache \
    --exclude=__pycache__ . | tar -xf -
  [ "$NOCHECK" != 1 ] || export DEB_BUILD_OPTIONS="${DEB_BUILD_OPTIONS:+$DEB_BUILD_OPTIONS }nocheck"
  dpkg-buildpackage -us -uc -b
  test -f "../$DEB"
  lintian --info --fail-on error "../$DEB"
  retry apt-get update -qq
  CASSINI_TEST_PACKAGE_MIGRATION=1 python3 -I /in/build-aux/ci/package_migration.py deb "../$DEB"
  cassini-talk --help >/dev/null
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
container_pull "$BASE"
# shellcheck disable=SC2086 # PKG_CACHE_MOUNTS is a list of run options
container_offline_smoke "$BASE" '
  . /ci/retry.sh
  printf "path-include=/usr/share/locale/*/LC_MESSAGES/cassini-talk.mo\n" > /etc/dpkg/dpkg.cfg.d/zz-cassini-locales
  printf "Acquire::Retries \"3\";\nAcquire::http::Timeout \"15\";\n" > /etc/apt/apt.conf.d/80retries
  retry apt-get update -qq
  retry apt-get install -y -qq --no-install-recommends "/d/$OUT" >/dev/null
  rm -rf /var/lib/apt/lists/*
' $PKG_CACHE_MOUNTS -v "$PWD/dist:/d:ro$Z" -v "$PWD/build-aux/ci":/ci:ro$Z -e OUT="$OUT" -e DEBIAN_FRONTEND=noninteractive
ls -l "dist/$OUT"
