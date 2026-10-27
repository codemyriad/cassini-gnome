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
