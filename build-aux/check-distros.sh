#!/bin/sh
# Build, test and smoke-test Cassini Viewer on several distributions, each in
# a clean container, using build-aux/install-deps.sh to get the packages.
#   build-aux/check-distros.sh [image ...]
# Default images: see DEFAULT_IMAGES below. Example, a single distribution:
#   build-aux/check-distros.sh fedora:latest
# Each image: install deps, meson setup/test/install, then check that GTK
# >= 4.18, libadwaita >= 1.8 and the GStreamer Ogg/Opus elements load from
# the installed tree. Prints a PASS/FAIL table; exits 1 if any image failed.
# Logs go to a temporary directory (printed at the end), not the source tree.
set -eu
case ${1:-} in -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;; esac
cd "$(dirname "$0")/.."
. build-aux/container.sh

DEFAULT_IMAGES="debian:testing ubuntu:26.04 fedora:latest
registry.opensuse.org/opensuse/tumbleweed:latest archlinux:latest alpine:edge"
[ $# -gt 0 ] || set -- $DEFAULT_IMAGES

LOGDIR=$(mktemp -d "${TMPDIR:-/tmp}/cassini-check-distros.XXXXXX")

# Runs inside the container, as root, with the source tree at /in (read-only).
# "==> step" lines mark progress; the summary reports the last one reached.
INNER='
set -eu
step() { printf "\\n==> %s\\n" "$1"; }
step "copy source"
mkdir -p /build/src
cp -a /in/. /build/src
cd /build/src
rm -rf _build* _install _flatpak dist obj-* .flatpak-builder debian/.debhelper debian/cassini-viewer
rm -rf src/cassini_viewer/__pycache__ tests/__pycache__

step "install-deps"
sh build-aux/install-deps.sh --all --yes

step "meson setup"
meson setup /build/b --prefix=/usr

step "meson test"
meson test -C /build/b --list > /build/tests.txt
cat /build/tests.txt
for t in pytest "Validate desktop file" "Validate appstream file" "Validate schema file"; do
  grep -q "$t" /build/tests.txt || { echo "missing test (tool not installed?): $t"; exit 1; }
done
meson test -C /build/b --print-errorlogs

step "meson install"
DESTDIR=/build/dest meson install -C /build/b --quiet
for f in usr/bin/cassini-viewer usr/share/cassini-viewer/cassini-viewer.gresource \
         usr/share/applications/com.gocassini.Viewer.desktop \
         usr/share/metainfo/com.gocassini.Viewer.metainfo.xml \
         usr/share/glib-2.0/schemas/com.gocassini.Viewer.gschema.xml \
         usr/share/cassini-viewer/cassini_viewer/main.py; do
  [ -e "/build/dest/$f" ] || { echo "not installed: /$f"; exit 1; }
done

step "system install"
# Without DESTDIR this also runs the post-install hooks (schemas, icon
# cache, desktop database), so their tools must be present.
command -v gtk4-update-icon-cache || command -v gtk-update-icon-cache ||
  { echo "no gtk4-update-icon-cache"; exit 1; }
meson install -C /build/b --quiet
[ -e /usr/share/glib-2.0/schemas/gschemas.compiled ]

step "runtime smoke test"
cd /
PYTHONPATH=/build/dest/usr/share/cassini-viewer python3 - <<"EOF"
import sys
assert sys.version_info >= (3, 11), "Python %s < 3.11" % sys.version.split()[0]
from cassini_viewer import colors, portable, runtime, summary, transcript  # GTK-free
import gi
for ns, v in [("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("Gst", "1.0"),
              ("Graphene", "1.0"), ("Pango", "1.0")]:
    gi.require_version(ns, v)
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gst, Gtk, Pango
Gst.init(None)
Gio.Resource.load("/build/dest/usr/share/cassini-viewer/cassini-viewer.gresource")
gtk = (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
adw = (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())
fmt = lambda v: ".".join(map(str, v))
info = "python %s, gtk %s, adw %s, gst %s" % (
    sys.version.split()[0], fmt(gtk), fmt(adw), fmt(Gst.version()[:3]))
print("@@versions", info)
problems = []
if gtk < (4, 18, 0): problems.append("GTK %s < 4.18" % fmt(gtk))
if adw < (1, 8, 0): problems.append("libadwaita %s < 1.8" % fmt(adw))
for el in ("oggdemux", "opusdec", "playbin"):
    if Gst.ElementFactory.find(el) is None:
        problems.append("GStreamer element %s missing" % el)
if problems:
    print("@@problems", "; ".join(problems))
    sys.exit(1)
runtime.check()  # same startup check as the launcher
EOF
step "ok"
'

results=
for image in "$@"; do
  log="$LOGDIR/$(image_slug "$image").log"
  echo
  echo "################ $image"
  { if container_run -e DEBIAN_FRONTEND=noninteractive -v "$PWD":/in:ro$Z \
         "$image" sh -c "$INNER"; then
      echo 0 > "$log.status"
    else
      echo $? > "$log.status"
    fi; } 2>&1 | tee "$log"
  step=$(sed -n 's/^==> //p' "$log" | tail -n 1)
  versions=$(sed -n 's/^@@versions //p' "$log" | tail -n 1)
  problems=$(sed -n 's/^@@problems //p' "$log" | tail -n 1)
  if [ "$(cat "$log.status")" = 0 ] && [ "$step" = ok ]; then
    verdict=PASS note=$versions
  else
    verdict=FAIL note="at '${step:-start}'${problems:+: $problems}${versions:+ ($versions)}"
  fi
  results="$results$verdict	$image	$note
"
done

echo
echo "################ summary (logs: $LOGDIR)"
printf '%s' "$results" | while IFS='	' read -r verdict image note; do
  printf '%-4s  %-50s %s\n' "$verdict" "$image" "$note"
done
case $results in *FAIL*) exit 1 ;; esac
