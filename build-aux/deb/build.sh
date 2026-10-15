#!/bin/sh
# Build the Debian package in a container and copy it to ./dist.
#   build-aux/deb/build.sh [base-image]      (default: ubuntu:26.04)
set -eu
cd "$(dirname "$0")/../.."
BASE=${1:-ubuntu:26.04}
TAG=cassini-viewer-deb:$(echo "$BASE" | tr ':/' '--')
docker build --network host -q -f build-aux/deb/Dockerfile --build-arg BASE="$BASE" -t "$TAG" .
mkdir -p dist
docker run --rm --network host -v "$PWD":/in:ro -v "$PWD/dist":/out "$TAG" sh -ec '
  mkdir -p /build/src && cp -a /in/. /build/src && cd /build/src
  rm -rf obj-* dist debian/.debhelper debian/cassini-viewer
  dpkg-buildpackage -us -uc -b
  lintian --info ../*.deb || true
  cp ../*.deb /out/
  chown "$(stat -c %u:%g /out)" /out/*.deb
'
ls -l dist/*.deb
