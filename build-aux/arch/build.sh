#!/bin/sh
# Build the Arch Linux package in a container and copy it to ./dist.
#   build-aux/arch/build.sh [base-image]      (default: archlinux:latest)
# Runs makepkg with check(), namcap (advisory) and an install smoke test.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
BASE=${1:-archlinux:latest}

meson_ver=$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" meson.build | head -n 1)
pkg_ver=$(sed -n 's/^pkgver=//p' build-aux/arch/PKGBUILD)
if [ "$meson_ver" != "$pkg_ver" ]; then
  echo "error: PKGBUILD pkgver ($pkg_ver) != meson.build version ($meson_ver)" >&2
  exit 1
fi

# The pinned writing-down artifacts, the PKGBUILD's other sources; makepkg
# finds them next to the PKGBUILD and checks their sha256sums again.
build-aux/whistle-cache.sh
build-aux/sherpa-cache.sh
mkdir -p dist
# Per base image, so parallel builds do not swap names.
name_file=.pkg-name-$(image_slug "$BASE")
container_run -e NAME_FILE="$name_file" -v "$PWD":/in:ro$Z -v "$PWD/dist":/out:rw$Z "$BASE" sh -ec '
  pacman -Syu --noconfirm --needed base-devel
  useradd -m builder
  echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder

  # Stage PKGBUILD plus a tarball of the source tree in a scratch dir.
  name=cassini-viewer-$1
  mkdir -p /build/pkg "/build/$name"
  cp -a /in/. "/build/$name"
  cd "/build/$name"
  rm -rf .whistle-cache .sherpa-cache .git _build* _install _flatpak ./*.flatpak build builddir dist obj-* .flatpak-builder repo .venv \
         .ruff_cache .pytest_cache debian/.debhelper debian/cassini-viewer \
    tests/recording/node_modules tests/recording/artifacts
  find . -name __pycache__ -prune -exec rm -rf {} +
  cd /build
  tar -czf "pkg/$name.tar.gz" "$name"
  cp "/in/build-aux/arch/PKGBUILD" /in/.whistle-cache/* /in/.sherpa-cache/* pkg/
  chown -R builder: /build/pkg

  cd /build/pkg
  su builder -c "makepkg -s --noconfirm"
  # The exact file this PKGBUILD produces, not whatever matches a glob.
  pkg=$(su builder -c "makepkg --packagelist" | grep -v -- "-debug-")
  [ "$(echo "$pkg" | wc -l)" = 1 ] && [ -f "$pkg" ] || { echo "unexpected package list: $pkg" >&2; exit 1; }

  if pacman -S --noconfirm --needed namcap >/dev/null 2>&1; then
    namcap PKGBUILD "$pkg" || true
  else
    echo "namcap not available; skipping"
  fi

  # Install and smoke-test.
  pacman -U --noconfirm "$pkg"
  test -x /usr/bin/cassini-viewer
  cassini-viewer --help >/dev/null   # runs the installed launcher headless
  python3 -I /in/build-aux/smoke-installed.py

  cp "$pkg" /out/
  chown "$(stat -c %u:%g /out)" "/out/${pkg##*/}" 2>/dev/null || true
  # Tell the clean-install step below which file this build produced.
  echo "${pkg##*/}" > "/out/$NAME_FILE"
' sh "$pkg_ver"
pkg_name=$(cat "dist/$name_file") && rm -f "dist/$name_file"

# The build container has makedepends and checkdepends installed, which can
# hide a missing dependency; install the package in a fresh container, then
# check it there with no network, writing the D11 goldens down offline.
container_offline_smoke "$BASE" '
  pacman -Syu --noconfirm --noprogressbar -q >/dev/null
  pacman -U --noconfirm --noprogressbar "/d/$PKG" >/dev/null
  pacman -Scc --noconfirm >/dev/null
' -v "$PWD/dist":/d:ro$Z -e PKG="$pkg_name"
ls -l "dist/$pkg_name"
