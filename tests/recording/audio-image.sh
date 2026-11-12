#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# The Recording audio-server image (tests/recording/Audio.Dockerfile) under a tag that hashes its inputs and
# ends with the ISO week, like tests/harness-image.sh: a changed Dockerfile or a new week names a new image, so a
# prebuilt copy (a pool host's image store, build-aux/ci/pool-prefetch.sh audio) is never older than a week.
#
#   tests/recording/audio-image.sh tag     cassini-recording-audio:<hash16>-<epoch>
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
epoch=${CASSINI_HARNESS_EPOCH:-$(date -u +%G-W%V)}
case ${1:-} in
  tag)
    hash=$(cat "$here/Audio.Dockerfile" "$here/audio-image.sh" | sha256sum | cut -c1-16)
    echo "cassini-recording-audio:$hash-$epoch"
    ;;
  *)
    echo "usage: $0 tag" >&2
    exit 2
    ;;
esac
