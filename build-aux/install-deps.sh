#!/bin/sh
# Install the packages needed to build and/or run Cassini Viewer, using the
# distribution's own package manager (apt, dnf, zypper, pacman or apk).
#   build-aux/install-deps.sh [--all|--build|--runtime] [--yes] [--dry-run]
#     --all       build and runtime dependencies (default)
#     --build     meson, ninja, gettext, glib tools, validators, pytest
#     --runtime   python3, PyGObject, GTK 4, libadwaita, GStreamer plugins
#     --yes       do not ask for confirmation
#     --dry-run   print the install command instead of running it
# The app needs GTK >= 4.18 and libadwaita >= 1.8; older distributions
# install fine but cannot run it (see build-aux/check-distros.sh).
# shellcheck disable=SC2086 # $y is an optional flag, deliberately unquoted
set -eu

set_build=0 set_runtime=0 dry_run=0 yes=0
for arg in "$@"; do
  case $arg in
    --all) set_build=1 set_runtime=1 ;;
    --build) set_build=1 ;;
    --runtime) set_runtime=1 ;;
    --dry-run|-n) dry_run=1 ;;
    --yes|-y) yes=1 ;;
    -h|--help) sed -n '2,11s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
[ $set_build = 0 ] && [ $set_runtime = 0 ] && set_build=1 set_runtime=1

unknown() {
  cat >&2 <<EOF
$1
Install these with your package manager (names vary between distributions):
  build:   meson (>= 1.0), ninja, python3, gettext (msgfmt),
           glib-compile-resources and glib-compile-schemas (glib dev tools),
           optional: desktop-file-validate, appstreamcli,
           gtk4-update-icon-cache, python3 pytest, jsonschema and
           rfc3339-validator (the protocol conformance tests)
  runtime: python3 (>= 3.11), PyGObject, GTK 4 (>= 4.18) and
           libadwaita (>= 1.8) with their GObject introspection typelibs,
           GStreamer 1.x typelib, gst-plugins-base and gst-plugins-good
           (oggdemux, opusdec), a PipeWire or PulseAudio GStreamer sink
Or use the Flatpak build: build-aux/flatpak/
EOF
  exit 1
}

[ -r /etc/os-release ] || unknown "error: /etc/os-release not found; cannot tell which distribution this is."
# shellcheck disable=SC1091
. /etc/os-release

family=
for id in ${ID:-} ${ID_LIKE:-}; do
  case $id in
    debian|ubuntu) family=apt ;;
    fedora|rhel|centos) family=dnf ;;
    opensuse*|suse|sles|sled) family=zypper ;;
    arch|archlinux|manjaro|endeavouros) family=pacman ;;
    alpine|postmarketos) family=apk ;;
  esac
  [ -n "$family" ] && break
done
[ -n "$family" ] || unknown "error: unsupported distribution '${ID:-?}' (ID_LIKE='${ID_LIKE:-}')."
if [ -e /run/ostree-booted ]; then
  unknown "error: this is an image-based system (ostree); install the packages inside a toolbox or distrobox container instead."
fi

case $family in
  apt)
    build="meson ninja-build python3 gettext libglib2.0-bin libglib2.0-dev-bin
           desktop-file-utils appstream libgtk-4-bin python3-pytest
           python3-jsonschema python3-rfc3339-validator"
    runtime="python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0
             gstreamer1.0-plugins-base gstreamer1.0-plugins-good" ;;
  dnf)
    build="meson ninja-build python3 gettext glib2 glib2-devel
           desktop-file-utils appstream gtk4-devel-tools python3-pytest
           python3-jsonschema python3-rfc3339-validator"
    runtime="python3 python3-gobject gtk4 libadwaita gstreamer1
             gstreamer1-plugins-base gstreamer1-plugins-good" ;;
  zypper)
    build="meson ninja python3 gettext-tools glib2-tools glib2-devel
           desktop-file-utils AppStream gtk4-tools python3-pytest
           python3-jsonschema python3-rfc3339-validator"
    runtime="python3 python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1
             typelib-1_0-Gst-1_0 gstreamer-plugins-base gstreamer-plugins-good" ;;
  pacman)
    build="meson ninja python gettext glib2 glib2-devel desktop-file-utils
           appstream gtk4 python-pytest pkgconf
           python-jsonschema python-rfc3339-validator"
    runtime="python python-gobject gtk4 libadwaita gstreamer
             gst-plugins-base gst-plugins-good" ;;
  apk)
    build="meson samurai python3 gettext glib glib-dev desktop-file-utils
           appstream gtk4.0 py3-pytest
           py3-jsonschema py3-rfc3339-validator"
    runtime="python3 py3-gobject3 gtk4.0 libadwaita gstreamer
             gst-plugins-base gst-plugins-good" ;;
esac

all=
[ $set_build = 1 ] && all="$all $build"
[ $set_runtime = 1 ] && all="$all $runtime"
set --
for p in $all; do
  case " $* " in *" $p "*) ;; *) set -- "$@" "$p" ;; esac
done

sudo=
if [ "$(id -u)" != 0 ]; then
  if command -v sudo >/dev/null 2>&1; then sudo=sudo
  elif command -v doas >/dev/null 2>&1; then sudo=doas
  elif [ $dry_run = 0 ]; then
    echo "error: not root and neither sudo nor doas is available" >&2; exit 1
  fi
fi

run() {
  if [ $dry_run = 1 ]; then
    echo ${sudo:+$sudo }"$*"
  else
    echo "+ ${sudo:+$sudo }$*" >&2
    $sudo "$@"
  fi
}

case $family in
  apt)
    run apt-get update
    if [ $yes = 1 ]; then
      run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
    else
      run apt-get install "$@"
    fi ;;
  dnf)
    [ $yes = 1 ] && y=-y || y=
    run dnf install $y "$@" ;;
  zypper)
    [ $yes = 1 ] && y=--non-interactive || y=
    run zypper $y install "$@" ;;
  pacman)
    # Arch does not support partial upgrades: sync and upgrade together.
    [ $yes = 1 ] && y=--noconfirm || y=
    run pacman -Syu --needed $y "$@" ;;
  apk)
    [ $yes = 1 ] && y= || y=--interactive
    run apk add --update-cache $y "$@" ;;
esac
