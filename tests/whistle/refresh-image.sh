#!/usr/bin/env bash
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
# A current-archive image belongs to one offline CI job (or standalone run).
# The weekly cached harness is an input; never remove it here.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
action=${1:?build, check or remove}
image=${2:?harness tag or refreshed image id}
job=${3:?offline harness job identity}
[[ $job =~ ^[a-zA-Z0-9_-]{1,128}$ ]] || { echo 'Invalid offline harness job identity' >&2; exit 2; }
label=io.codemyriad.cassini.offline-job
case "$action" in
  build)
    echo "Offline harness: refreshing current archive for $job" >&2
    image=$("$root/build-aux/ci/docker-build-bounded.sh" "offline current-archive refresh" docker build \
      --no-cache --label "$label=$job" --build-arg HARNESS="$image" - < "$root/tests/whistle/Containerfile")
    echo "Offline harness: refreshed $image for $job" >&2
    echo "$image"
    ;;
  check | remove)
    [[ $image =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Expected an exact refreshed image id' >&2; exit 2; }
    owner=$(docker image inspect -f "{{index .Config.Labels \"$label\"}}" "$image")
    [ "$owner" = "$job" ] || { echo 'Refreshed offline harness belongs to another job' >&2; exit 1; }
    if [ "$action" = remove ]; then docker image rm "$image" >&2; else echo "$image"; fi
    ;;
  *) echo 'Expected build, check or remove' >&2; exit 2 ;;
esac
