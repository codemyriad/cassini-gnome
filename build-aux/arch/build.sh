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

mkdir -p dist
container_run -v "$PWD":/in:ro$Z -v "$PWD/dist":/out:rw$Z "$BASE" sh -ec '
  pacman -Syu --noconfirm --needed base-devel
  useradd -m builder
  echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder

  # Stage PKGBUILD plus a tarball of the source tree in a scratch dir.
  name=cassini-viewer-$1
  mkdir -p /build/pkg "/build/$name"
  cp -a /in/. "/build/$name"
  cd "/build/$name"
  rm -rf .git _build* _install _flatpak ./*.flatpak build builddir dist obj-* .flatpak-builder repo .venv \
         .ruff_cache .pytest_cache debian/.debhelper debian/cassini-viewer
  find . -name __pycache__ -prune -exec rm -rf {} +
  cd /build
  tar -czf "pkg/$name.tar.gz" "$name"
  cp "/in/build-aux/arch/PKGBUILD" pkg/
  chown -R builder: /build/pkg

  cd /build/pkg
  su builder -c "makepkg -s --noconfirm"
  pkg=$(ls /build/pkg/*.pkg.tar.zst)

  if pacman -S --noconfirm --needed namcap >/dev/null 2>&1; then
    namcap PKGBUILD "$pkg" || true
  else
    echo "namcap not available; skipping"
  fi

  # Install and smoke-test.
  pacman -U --noconfirm "$pkg"
  test -x /usr/bin/cassini-viewer
  cassini-viewer --help >/dev/null   # runs the installed launcher headless
  python3 -c "import sys; sys.path.insert(0, \"/usr/share/cassini-viewer\"); import cassini_viewer.portable"
  python3 -c "
import gi
gi.require_version(\"Gtk\", \"4.0\"); gi.require_version(\"Adw\", \"1\"); gi.require_version(\"Gst\", \"1.0\")
from gi.repository import Gtk, Adw, Gst
print(\"GTK %d.%d.%d\" % (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()))
print(\"libadwaita %d.%d.%d\" % (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()))
print(Gst.version_string())
"
  for el in oggdemux opusdec playbin; do gst-inspect-1.0 --exists "$el" || { echo "missing GStreamer element $el" >&2; exit 1; }; done

  cp "$pkg" /out/
  chown "$(stat -c %u:%g /out)" "/out/${pkg##*/}" 2>/dev/null || true
' sh "$pkg_ver"
ls -l dist/*.pkg.tar.zst
