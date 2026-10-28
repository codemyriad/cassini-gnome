#!/bin/sh
# shellcheck disable=SC2034 # $Z is used by the scripts that source this file
# Shared helpers for the container build scripts in build-aux/.
# Source it; do not run it:   . "$(dirname "$0")/../container.sh"
#
# Uses podman or docker, whichever is installed (podman first).
# Set CONTAINER_ENGINE=docker or CONTAINER_ENGINE=podman to choose.

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

# Run a container. Rootless podman maps the caller's user into the container,
# so files written to bind mounts are already owned by the caller; with docker
# the scripts chown their output themselves.
container_run() {
  "$CONTAINER_ENGINE" run --rm --network host "$@"
}

# Append to bind-mount options so SELinux hosts (Fedora, RHEL) let the
# container read them:   -v "$PWD":/in:ro$Z   -v "$PWD/dist":/out:rw$Z
Z=
if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
  Z=,z
fi

container_build() {
  "$CONTAINER_ENGINE" build --network host "$@"
}

# A tag-safe name for an image reference: fedora:43 -> fedora-43
image_slug() {
  echo "$1" | tr ':/@' '---'
}

# Run a container with no network at all: only loopback.
container_run_offline() {
  "$CONTAINER_ENGINE" run --rm --network none "$@"
}

# The clean-install smoke test of a built package: run INSTALL (a sh script)
# in a fresh BASE container, with the network, to install the package and its
# dependencies; keep the result as a local image (cassini-pkgtest-installed:*,
# never pushed); then start that image with --network none and a read-only
# root, and smoke-test the installed app there, including writing the D11
# goldens down offline (build-aux/smoke-installed.py --whistle).
#   container_offline_smoke BASE INSTALL [run options for the install step...]
container_offline_smoke() {
  smoke_base=$1 smoke_install=$2
  shift 2
  smoke_name=cassini-pkgtest-install-$(image_slug "$smoke_base")-$$
  smoke_image=cassini-pkgtest-installed:$(image_slug "$smoke_base")-$$
  "$CONTAINER_ENGINE" rm -f "$smoke_name" >/dev/null 2>&1 || true
  if ! "$CONTAINER_ENGINE" run --name "$smoke_name" --network host "$@" "$smoke_base" sh -ec "$smoke_install"; then
    "$CONTAINER_ENGINE" rm -f "$smoke_name" >/dev/null 2>&1 || true
    return 1
  fi
  "$CONTAINER_ENGINE" commit "$smoke_name" "$smoke_image" >/dev/null
  "$CONTAINER_ENGINE" rm "$smoke_name" >/dev/null
  smoke_status=0
  container_run_offline --read-only --tmpfs /tmp:rw,exec,nosuid,nodev -e HOME=/tmp \
    -v "$PWD/build-aux:/aux:ro$Z" -v "$PWD/tests/whistle:/goldens:ro$Z" "$smoke_image" sh -ec '
      cassini-viewer --help >/dev/null
      python3 -I /aux/smoke-installed.py --whistle /goldens
      echo "clean install smoke test passed, offline"
    ' || smoke_status=$?
  "$CONTAINER_ENGINE" rmi "$smoke_image" >/dev/null 2>&1 || true
  return $smoke_status
}
