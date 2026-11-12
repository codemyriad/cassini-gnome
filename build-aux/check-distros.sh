#!/bin/sh
# Build, test and smoke-test Cassini talk on several distributions, each in
# a clean container, using build-aux/install-deps.sh to get the packages.
#   build-aux/check-distros.sh [image ...]
# Default images: see DEFAULT_IMAGES below. Example, a single distribution:
#   build-aux/check-distros.sh fedora:latest
# Each image: install deps, meson setup/test/install, then check that GTK
# >= 4.18, libadwaita >= 1.8 and all playback and recording GStreamer elements
# load from the installed tree. Prints a PASS/FAIL table; exits 1 if any failed.
# Logs go to a temporary directory (printed at the end), not the source tree;
# with CASSINI_TEST_LOGS set, each image's meson-logs also go there.
# CASSINI_SKIP_PYTEST=1 leaves out only the pytest suite (CI, where a package
# job runs it in the same image); every other step and Meson test still runs.
set -eu
case ${1:-} in -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;; esac
cd "$(dirname "$0")/.."
. build-aux/container.sh

DEFAULT_IMAGES="debian:testing@sha256:2d46f43b677a047cfab870549eea0d83d0a9bea2f55b3f481efc901043916c2a ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96 fedora:latest@sha256:a7d72ba2745b100a3351087472e5ee7b2450af7cf331e7673007e3817612839c
registry.opensuse.org/opensuse/tumbleweed:latest@sha256:27356084647aa08ad9e9c5e0ebfd2742f9f65279860e862b3a65148450c113da archlinux:latest@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899 alpine:edge@sha256:020dfcbaaf4cc1078bf2d9c7ba31a8466e334061dcd2f248001d68f79e52c000"
[ $# -gt 0 ] || set -- $DEFAULT_IMAGES

LOGDIR=$(mktemp -d "${TMPDIR:-/tmp}/cassini-check-distros.XXXXXX")
OUTCOME_ROOT=${CASSINI_INSTALL_OUTCOME:-$LOGDIR/install-outcome}
ALPINE_INSTALL_CTR="cassini-distro-install-alpine-edge-$$"
ALPINE_INSTALLED_IMG="cassini-distro-installed:alpine-edge-$$"

cleanup_alpine_stage() {
  "$CONTAINER_ENGINE" rm -f "$ALPINE_INSTALL_CTR" >/dev/null 2>&1 || true
  "$CONTAINER_ENGINE" rmi "$ALPINE_INSTALLED_IMG" >/dev/null 2>&1 || true
}
trap cleanup_alpine_stage EXIT

# Stage 1 for alpine:edge: isolate apk package installation in its own
# container before any build or test container runs, recording the trusted
# outcome outside test-writable /logs and making it read-only on exit.
ALPINE_INSTALL_INNER='
set -eu
step() { printf "\\n==> %s\\n" "$1"; }
lock_outcome() {
  [ -d /install-outcome ] || return 0
  owner=$(stat -c %u:%g /install-outcome)
  chown -R "$owner" /install-outcome
  chmod 0444 /install-outcome/* 2>/dev/null || true
  chmod 0555 /install-outcome
}
trap "lock_outcome || true" EXIT
step "install-deps"
cd /in
sh build-aux/ci/alpine_edge_install.sh || {
  status=$?
  if [ "$status" = 75 ] && [ -f /install-outcome/infra-skipped.json ]; then
    step "infra-skip-install-deps"
    exit 75
  fi
  exit "$status"
}
touch /etc/cassini-deps-installed
'

# Runs inside the container, as root, with the source tree at /in (read-only).
# "==> step" lines mark progress; the summary reports the last one reached.
INNER='
set -eu
. /in/build-aux/ci/retry.sh
step() { printf "\\n==> %s\\n" "$1"; }
# Keep the Meson test logs, also when a test fails (test_log_dir).
save_logs() {
  [ -d /logs ] || return 0
  [ ! -d /build/b/meson-logs ] || cp -a /build/b/meson-logs/. /logs/
  cp /etc/os-release /logs/os-release  # G11: the image that ran the tests
  chown -R "$(stat -c %u:%g /logs)" /logs
}
trap "save_logs || true" EXIT
step "copy source"
mkdir -p /build/src
cp -a /in/. /build/src
cd /build/src
rm -rf _build* _install _flatpak dist obj-* .flatpak-builder debian/.debhelper debian/cassini-talk \
  .git .venv tests/recording/node_modules tests/recording/artifacts
rm -rf src/cassini_talk/__pycache__ tests/__pycache__

# Only these two steps reach the network: package installs, retried.
step "align container snapshot"
retry sh build-aux/ci/prepare_tumbleweed.sh

step "install-deps"
# Fedora recommends GPU drivers, portals and audio daemons that this headless
# source gate never uses. Keep every declared runtime/build dependency, the
# full suite and the strict plugin/startup smoke below. Other images retain
# their existing package selection.
if [ ! -f /etc/cassini-deps-installed ]; then
  if command -v dnf >/dev/null 2>&1; then
    retry sh build-aux/install-deps.sh --all --yes --required-only
  else
    retry sh build-aux/install-deps.sh --all --yes
  fi
fi

step "meson setup"
meson setup /build/b --prefix=/usr -Dpytest_parallel=true

step "meson test"
# Every test registered, from the introspection JSON (the --list text
# differs between Meson releases): pytest-1 .. pytest-N, conformance, Validate.
meson introspect /build/b --tests > /build/tests.json
python3 build-aux/ci/check_meson_tests.py /build/tests.json
if [ "${CASSINI_SKIP_PYTEST:-}" = 1 ]; then
  meson test -C /build/b --print-errorlogs --no-suite pytest
else
  meson test -C /build/b --num-processes=2 --print-errorlogs
fi

step "meson install"
DESTDIR=/build/dest meson install -C /build/b --quiet
for f in usr/bin/cassini-talk usr/bin/cassini-browser-host usr/bin/cassini-browser-control-host \
         usr/share/cassini-talk/browser-extension/manifest.json usr/share/cassini-talk/cassini-talk.gresource \
         usr/share/applications/io.codemyriad.CassiniTalk.desktop \
         usr/share/metainfo/io.codemyriad.CassiniTalk.metainfo.xml \
         usr/share/glib-2.0/schemas/io.codemyriad.CassiniTalk.gschema.xml \
         usr/share/cassini-talk/cassini_talk/main.py; do
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
PYTHONPATH=/build/dest/usr/share/cassini-talk python3 - <<"EOF"
import sys
assert sys.version_info >= (3, 11), "Python %s < 3.11" % sys.version.split()[0]
from cassini_talk import portable, runtime, summary, transcript  # GTK-free
import gi
for ns, v in [("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("Gst", "1.0"),
              ("Graphene", "1.0"), ("Pango", "1.0")]:
    gi.require_version(ns, v)
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gst, Gtk, Pango
Gst.init(None)
Gio.Resource.load("/build/dest/usr/share/cassini-talk/cassini-talk.gresource")
gtk = (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
adw = (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())
fmt = lambda v: ".".join(map(str, v))
info = "python %s, gtk %s, adw %s, gst %s" % (
    sys.version.split()[0], fmt(gtk), fmt(adw), fmt(Gst.version()[:3]))
print("@@versions", info)
problems = []
if gtk < (4, 18, 0): problems.append("GTK %s < 4.18" % fmt(gtk))
if adw < (1, 8, 0): problems.append("libadwaita %s < 1.8" % fmt(adw))
# Every playback and recording element: the app only warns about these.
for el in runtime.missing_gst_elements(Gst.ElementFactory.find, runtime.all_elements()):
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
  outcome_dir="$OUTCOME_ROOT/distro-$(image_slug "$image")"
  echo 1 > "$log.status"
  echo
  echo "################ $image"
  {
    stage_ok=1
    run_image="$image"
    # A prebuilt image with install-deps.sh already run (build-aux/ci/ci-image.sh, GHCR) when one exists for
    # these exact inputs; alpine:edge keeps its own isolated install stage below.
    case $image in alpine:*) ;; *) run_image=$(build-aux/ci/ci-image.sh use source "$image") ;; esac
    if [ "$run_image" = "$image" ] && ! container_pull "$image"; then
      echo 1 > "$log.status"
      stage_ok=0
    elif [ "${image%@sha256:*}" = "alpine:edge" ]; then
      chmod -R u+w "$outcome_dir" 2>/dev/null || true
      rm -rf "$outcome_dir"
      mkdir -p "$outcome_dir"
      "$CONTAINER_ENGINE" rm -f "$ALPINE_INSTALL_CTR" >/dev/null 2>&1 || true
      install_rc=0
      "$CONTAINER_ENGINE" run --name "$ALPINE_INSTALL_CTR" --network host \
        -e ALPINE_INSTALL_OUTCOME_DIR=/install-outcome \
        -v "$PWD":/in:ro$Z -v "$outcome_dir:/install-outcome:rw$Z" \
        "$image" sh -c "$ALPINE_INSTALL_INNER" || install_rc=$?
      if [ "$install_rc" = 0 ]; then
        if "$CONTAINER_ENGINE" commit "$ALPINE_INSTALL_CTR" "$ALPINE_INSTALLED_IMG" >/dev/null; then
          "$CONTAINER_ENGINE" rm -f "$ALPINE_INSTALL_CTR" >/dev/null 2>&1 || true
          run_image="$ALPINE_INSTALLED_IMG"
        else
          "$CONTAINER_ENGINE" rm -f "$ALPINE_INSTALL_CTR" >/dev/null 2>&1 || true
          echo 1 > "$log.status"
          stage_ok=0
        fi
      else
        "$CONTAINER_ENGINE" rm -f "$ALPINE_INSTALL_CTR" >/dev/null 2>&1 || true
        stage_ok=0
        if [ "$install_rc" = 75 ] && [ -f "$outcome_dir/infra-skipped.json" ] && [ ! -e "$outcome_dir/outcome.json" ]; then
          echo 0 > "$log.status"
        else
          echo "$install_rc" > "$log.status"
        fi
      fi
    fi
    if [ "$stage_ok" = 1 ]; then
      logs=$(test_log_dir "distro-$(image_slug "$image")")
      if container_run -e DEBIAN_FRONTEND=noninteractive -e CASSINI_SKIP_PYTEST="${CASSINI_SKIP_PYTEST:-}" -v "$PWD":/in:ro$Z \
           ${logs:+-v "$logs:/logs:rw$Z"} "$run_image" sh -c "$INNER"; then
        echo 0 > "$log.status"
      else
        echo $? > "$log.status"
      fi
      if [ "$run_image" = "$ALPINE_INSTALLED_IMG" ]; then
        "$CONTAINER_ENGINE" rmi "$ALPINE_INSTALLED_IMG" >/dev/null 2>&1 || true
      fi
    fi
  } 2>&1 | tee "$log"
  step=$(sed -n 's/^==> //p' "$log" | tail -n 1)
  versions=$(sed -n 's/^@@versions //p' "$log" | tail -n 1)
  problems=$(sed -n 's/^@@problems //p' "$log" | tail -n 1)
  if [ "$(cat "$log.status")" = 0 ] && [ "$step" = ok ]; then
    verdict=PASS note=$versions
  elif [ "${image%@sha256:*}" = "alpine:edge" ] && [ "$(cat "$log.status")" = 0 ] && [ "$step" = "infra-skip-install-deps" ] && [ -f "$outcome_dir/infra-skipped.json" ] && [ ! -e "$outcome_dir/outcome.json" ]; then
    verdict=SKIP note="upstream apk install failure on primary and fallback mirrors"
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
