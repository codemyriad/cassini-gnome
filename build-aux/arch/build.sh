#!/bin/sh
# Build the Arch Linux package in a container and copy it to ./dist.
#   build-aux/arch/build.sh [base-image]      (default: archlinux:latest)
# Runs makepkg with check(), namcap (advisory) and an install smoke test.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
BASE=${1:-archlinux:latest@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899}

meson_ver=$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" meson.build | head -n 1)
pkg_ver=$(sed -n 's/^pkgver=//p' build-aux/arch/PKGBUILD)
expected_pkg_ver=$(printf '%s\n' "$meson_ver" | sed 's/~beta\.\([1-9][0-9]*\)$/beta\1/')
if [ "$expected_pkg_ver" != "$pkg_ver" ]; then
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
logs=$(test_log_dir "arch-$(image_slug "$BASE")")
BUILD_IMAGE=$(python3 build-aux/ci/ci_images.py pull arch "$BASE" || true)
if [ -z "$BUILD_IMAGE" ]; then
  container_pull "$BASE"
  BUILD_IMAGE=$BASE
fi
# Only the package installs reach the network; each is retried (retry.sh).
container_run -e NAME_FILE="$name_file" -v "$PWD":/in:ro$Z -v "$PWD/dist":/out:rw$Z ${logs:+-v "$logs:/logs:rw$Z"} "$BUILD_IMAGE" sh -ec '
  . /in/build-aux/ci/retry.sh
  # Keep the Meson test logs, also when check() fails (test_log_dir).
  save_logs() {
    [ -d /logs ] || return 0
    [ ! -d /build/pkg/src/build/meson-logs ] || cp -a /build/pkg/src/build/meson-logs/. /logs/
    cp /etc/os-release /logs/os-release  # G11: the image that ran the tests
    chown -R "$(stat -c %u:%g /logs)" /logs
  }
  trap "save_logs || true" EXIT
  if [ ! -f /etc/cassini-arch-deps-installed ]; then
    # The minimal image excludes non-English MO files. Preserve Cassini
    # catalogs for the real installed-language smoke test.
    printf "\nNoExtract = !usr/share/locale/*/LC_MESSAGES/cassini-talk.mo\n" >> /etc/pacman.conf
    retry pacman -Syu --noconfirm --needed base-devel
    useradd -m builder
    echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
  fi

  # Stage PKGBUILD plus a tarball of the source tree in a scratch dir.
  name=cassini-talk-$1
  mkdir -p /build/pkg "/build/$name"
  cp -a /in/. "/build/$name"
  cd "/build/$name"
  rm -rf .whistle-cache .sherpa-cache .git _build* _install _flatpak ./*.flatpak build builddir dist obj-* .flatpak-builder repo .venv \
         .ruff_cache .pytest_cache debian/.debhelper debian/cassini-talk debian/tmp \
    tests/recording/node_modules tests/recording/artifacts
  find . -name __pycache__ -prune -exec rm -rf {} +
  cd /build
  tar -czf "pkg/$name.tar.gz" "$name"
  cp "/in/build-aux/arch/PKGBUILD" /in/.whistle-cache/* /in/.sherpa-cache/* pkg/
  chown -R builder: /build/pkg

  cd /build/pkg
  if [ ! -f /etc/cassini-arch-deps-installed ]; then
    # Install the PKGBUILD dependencies first, retried, so makepkg -s (which
    # also builds and runs check()) finds them all and never downloads.
    deps=$(su builder -c "makepkg --printsrcinfo" | sed -n "s/^\t\(make\|check\)\{0,1\}depends = //p")
    # Word splitting is wanted: one argument per dependency.
    retry pacman -S --noconfirm --needed --asdeps $deps
  fi
  su builder -c "makepkg -s --noconfirm"
  # The exact file this PKGBUILD produces, not whatever matches a glob.
  pkg=$(su builder -c "makepkg --packagelist" | grep -v -- "-debug-")
  [ "$(echo "$pkg" | wc -l)" = 1 ] && [ -f "$pkg" ] || { echo "unexpected package list: $pkg" >&2; exit 1; }

  if command -v namcap >/dev/null 2>&1 || retry pacman -S --noconfirm --needed namcap >/dev/null; then
    namcap PKGBUILD "$pkg" || true
  else
    echo "namcap not available; skipping"
  fi

  # Install and smoke-test.
  CASSINI_TEST_PACKAGE_MIGRATION=1 python3 -I /in/build-aux/ci/package_migration.py arch "$pkg"
  test -x /usr/bin/cassini-talk
  cassini-talk --help >/dev/null   # runs the installed launcher headless
  python3 -I /in/build-aux/smoke-installed.py
  python3 -I /in/build-aux/ci/validate_installed.py "$2"

  cp "$pkg" /out/
  chown "$(stat -c %u:%g /out)" "/out/${pkg##*/}" 2>/dev/null || true
  # Tell the clean-install step below which file this build produced.
  echo "${pkg##*/}" > "/out/$NAME_FILE"
' sh "$pkg_ver" "$meson_ver"
pkg_name=$(cat "dist/$name_file") && rm -f "dist/$name_file"

# The build container has makedepends and checkdepends installed, which can
# hide a missing dependency; install the package in a fresh container, then
# check it there with no network, writing the D11 goldens down offline.
container_pull "$BASE"
container_offline_smoke "$BASE" '
  . /ci/retry.sh
  printf "\nNoExtract = !usr/share/locale/*/LC_MESSAGES/cassini-talk.mo\n" >> /etc/pacman.conf
  retry pacman -Syu --noconfirm --noprogressbar -q >/dev/null
  retry pacman -U --noconfirm --noprogressbar "/d/$PKG" >/dev/null
  pacman -Scc --noconfirm >/dev/null
' -v "$PWD/dist":/d:ro$Z -v "$PWD/build-aux/ci":/ci:ro$Z -e PKG="$pkg_name"
ls -l "dist/$pkg_name"
