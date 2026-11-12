#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run a build or Meson-test command inside $CASSINI_BUILD_IMAGE at $PWD,
# matching the caller's host UID/GID under rootful Docker so build outputs and
# caches stay caller-owned without root-owned leftovers on self-hosted runners.
set -eu
[ "$#" -ge 1 ] || { echo "usage: $0 command [args...]" >&2; exit 2; }
[ -n "${CASSINI_BUILD_IMAGE:-}" ] || { echo "error: CASSINI_BUILD_IMAGE is not set" >&2; exit 2; }

if [ -z "${CONTAINER_ENGINE:-}" ]; then
  if command -v podman >/dev/null 2>&1; then
    CONTAINER_ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    CONTAINER_ENGINE=docker
  else
    echo "error: neither podman nor docker is installed" >&2
    exit 1
  fi
fi

Z=
if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
  Z=,z
fi

work=$PWD
host_uid=0
host_gid=0
if [ "$CONTAINER_ENGINE" = docker ]; then
  host_uid=$(id -u)
  host_gid=$(id -g)
fi

exec "$CONTAINER_ENGINE" run --rm --network host \
  -v "$work:$work$Z" -w "$work" \
  -e HOST_UID="$host_uid" -e HOST_GID="$host_gid" \
  ${PYTEST_ADDOPTS:+-e PYTEST_ADDOPTS="$PYTEST_ADDOPTS"} \
  ${CASSINI_PERF_GATE:+-e CASSINI_PERF_GATE="$CASSINI_PERF_GATE"} \
  ${GITHUB_SHA:+-e GITHUB_SHA="$GITHUB_SHA"} \
  "$CASSINI_BUILD_IMAGE" python3 -I -c '
import grp, os, pwd, sys
uid, gid = int(os.environ.pop("HOST_UID")), int(os.environ.pop("HOST_GID"))
home = "/tmp/cassini-home"
os.makedirs(home, mode=0o700, exist_ok=True)
os.chmod(home, 0o700)
if uid != 0:
    os.chown(home, uid, gid)
    try:
        grp.getgrgid(gid)
    except KeyError:
        with open("/etc/group", "a", encoding="utf-8") as fh:
            fh.write(f"builder:x:{gid}:\n")
    try:
        pwd.getpwuid(uid)
    except KeyError:
        with open("/etc/passwd", "a", encoding="utf-8") as fh:
            fh.write(f"builder:x:{uid}:{gid}::{home}:/bin/sh\n")
    os.setgid(gid)
    os.setgroups([gid])
    os.setuid(uid)
os.environ["HOME"] = home
os.execvp(sys.argv[1], sys.argv[1:])
' "$@"
