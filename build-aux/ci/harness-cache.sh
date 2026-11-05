#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# C (D66): CI's cache of the test harness images tests/harness-image.sh names.
# Main saves it, pull requests only restore it (actions/cache/restore), and its
# key is the images' tags: a new week, a moved base or any changed input is a new key.
#
#   harness-cache.sh key NAME...        key=... and path=... for $GITHUB_OUTPUT; pins the epoch and
#                                       the bases' digests in $GITHUB_ENV
#   harness-cache.sh load DIR           docker load every image saved in DIR
#   harness-cache.sh save DIR NAME...   docker save each NAME's current harness image into DIR
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)

case "${1:-}" in
  key)
    shift
    # One epoch for the whole job, so that run.sh asks for the tags this key holds.
    export CASSINI_HARNESS_EPOCH=${CASSINI_HARNESS_EPOCH:-$(date -u +%G-W%V)}
    # One resolution of each base's digest for the whole job, so that run.sh builds
    # from the base this key names: a base moving mid-job cannot split them.
    export CASSINI_HARNESS_BASES
    CASSINI_HARNESS_BASES=$("$root/tests/harness-image.sh" pin "$@")
    if [ -n "${GITHUB_ENV:-}" ]; then
      printf 'CASSINI_HARNESS_EPOCH=%s\nCASSINI_HARNESS_BASES=%s\n' "$CASSINI_HARNESS_EPOCH" "$CASSINI_HARNESS_BASES" >>"$GITHUB_ENV"
    fi
    tags=$(for name in "$@"; do "$root/tests/harness-image.sh" tag "$name"; done)
    names=$(IFS=-; echo "$*")
    echo "key=harness-$names-$CASSINI_HARNESS_EPOCH-$(printf '%s\n' "$tags" | sha256sum | cut -c1-16)"
    echo "path=${RUNNER_TEMP:-/tmp}/harness-images"
    ;;
  load)
    for image in "${2:?directory}"/*.tar; do
      [ -e "$image" ] || continue
      docker load -q -i "$image"
    done
    ;;
  save)
    directory=${2:?directory}
    shift 2
    mkdir -p "$directory"
    for name in "$@"; do
      tag=$("$root/tests/harness-image.sh" tag "$name")
      # Only an image this job built or loaded; never a stale one under a new key.
      docker image inspect "$tag" >/dev/null
      docker save -o "$directory/$name.tar" "$tag"
    done
    ;;
  *)
    echo "usage: $0 key NAME... | load DIR | save DIR NAME..." >&2
    exit 2
    ;;
esac
