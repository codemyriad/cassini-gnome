#!/bin/sh
# Build the RPM in a container and copy it (and the SRPM) to ./dist.
#   build-aux/rpm/build.sh [base-image]      (default: fedora:latest)
# Works with Fedora (dnf) and openSUSE (zypper) images, e.g.
#   build-aux/rpm/build.sh registry.opensuse.org/opensuse/tumbleweed:latest
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
BASE=${1:-fedora:latest}
VERSION=$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" meson.build | head -n1)
SPEC_VERSION=$(sed -n 's/^Version: *//p' build-aux/rpm/cassini-viewer.spec)
if [ "$VERSION" != "$SPEC_VERSION" ]; then
  echo "error: meson.build says $VERSION but the spec says $SPEC_VERSION" >&2
  exit 1
fi
mkdir -p dist
container_run -e VERSION="$VERSION" -v "$PWD":/in:ro$Z -v "$PWD/dist":/out:rw$Z "$BASE" sh -ec '
  top=/build/rpmbuild src=/build/cassini-viewer-$VERSION
  mkdir -p $top/SOURCES $top/SPECS "$src"
  spec=$top/SPECS/cassini-viewer.spec
  install -m 644 /in/build-aux/rpm/cassini-viewer.spec "$spec"
  if command -v dnf >/dev/null 2>&1; then
    dnf -y install rpm-build tar xz "dnf-command(builddep)"
    dnf -y builddep "$spec"
    dnf -y install rpmlint || true
    install_rpm() { dnf -y install "$@"; }
  elif command -v zypper >/dev/null 2>&1; then
    zypper -n install rpm-build tar xz
    rpmspec -q --buildrequires "$spec" | tr -d " " | xargs zypper -n install
    zypper -n install rpmlint || true
    install_rpm() { zypper -n install --allow-unsigned-rpm "$@"; }
  else
    echo "error: no dnf or zypper in this image" >&2; exit 1
  fi

  cp -a /in/. "$src"
  cd "$src"
  rm -rf .git _build* _install _flatpak build builddir dist repo .flatpak-builder .venv obj-* \
    .pytest_cache .ruff_cache debian/.debhelper debian/cassini-viewer *.flatpak
  find . -name __pycache__ -prune -exec rm -rf {} +
  cd /build
  tar -cJf $top/SOURCES/cassini-viewer-$VERSION.tar.xz cassini-viewer-$VERSION

  # openSUSE defines no %{dist}; tag its RPMs with the distro so they do not
  # clash in dist/ with builds for other distros.
  dist=$(rpm --eval "%{?dist}")
  if [ -z "$dist" ]; then
    dist=.$(. /etc/os-release; echo "$ID${VERSION_ID:+.$VERSION_ID}" | tr -- "-" "_")
    case $dist in .opensuse_tumbleweed*) dist=.opensuse_tumbleweed ;; esac
  fi
  rpmbuild -ba --define "_topdir $top" --define "dist $dist" "$spec"
  if command -v rpmlint >/dev/null 2>&1; then
    rpmlint $top/RPMS/noarch/*.rpm $top/SRPMS/*.rpm || true
  fi

  install_rpm $top/RPMS/noarch/*.rpm
  test -x /usr/bin/cassini-viewer
  head -n1 /usr/bin/cassini-viewer | grep -qx "#!/usr/bin/python3"
  python3 -c "import sys; sys.path.insert(0, \"/usr/share/cassini-viewer\"); import cassini_viewer.portable"
  python3 -c "
import gi
gi.require_version(\"Gtk\", \"4.0\"); gi.require_version(\"Adw\", \"1\"); gi.require_version(\"Gst\", \"1.0\")
from gi.repository import Adw, Gst, Gtk
Gst.init(None)
missing = [e for e in (\"oggdemux\", \"opusdec\", \"playbin\") if not Gst.ElementFactory.find(e)]
print(\"GTK %d.%d, libadwaita %d.%d, %s\" % (Gtk.get_major_version(), Gtk.get_minor_version(),
      Adw.get_major_version(), Adw.get_minor_version(), Gst.version_string()))
assert not missing, \"missing GStreamer elements: %s\" % missing
"
  echo "smoke test passed"

  cp $top/RPMS/noarch/*.rpm $top/SRPMS/*.rpm /out/
  chown "$(stat -c %u:%g /out)" /out/*.rpm 2>/dev/null || true
'
ls -l dist/*.rpm
