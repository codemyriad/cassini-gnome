#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Use what a self-hosted pool host prepared when its snapshot was baked, instead of downloading, restoring or
# building it in this job (GNOME-CI-CACHING.md items 1 and 4). Prints ITEM=true for each ITEM it provided and
# ITEM=false otherwise, for $GITHUB_OUTPUT; the workflow's own cache and build steps then run only for what is
# false. Never fails: on GitHub-hosted runners, or on a host without the prefetch, everything is false and the
# job works exactly as before.
#
#   build-aux/ci/pool-prefetch.sh ITEM...
#     playwright    $RUNNER_TEMP/ms-playwright   Playwright browsers (the job still runs `playwright install`)
#     models        $RUNNER_TEMP/{parakeet-models,nemotron-models,voice-models,diarization-fixtures}
#     build-caches  .whistle-cache .sherpa-cache
#     flatpak       .desktop-flatpak-cache       a Flatpak user installation with org.gnome.Platform//50
#     harness:NAME  the harness image tests/harness-image.sh tag NAME names (needs CASSINI_HARNESS_EPOCH and
#                   CASSINI_HARNESS_BASES, as set by harness-cache.sh key)
#     audio         the Recording audio-server image tests/recording/audio-image.sh tag names
#
# The host's copies sit in a private copy-on-write overlay of the tool cache, so the job may write into them.
# They are checked like CI's own caches: models and build pins by sha256 in the prepare scripts (a stale copy
# is re-downloaded), images by a tag that hashes their inputs and the ISO week (a stale host has no image of
# that tag, so the job builds it), Playwright by its own browser revision check, and the Flatpak runtime is
# installed again by tests/desktop/run.sh. PREFETCH.json records the cassini-gnome commit the host used.
set -uo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tc=${RUNNER_TOOL_CACHE:-/nonexistent}/cassini
store=${GHA_IMAGE_STORE:-}
link() {  # host-dir job-path: use the host's copy at the job's path
  [ -d "$1" ] || return 1
  [ -e "$2" ] && [ ! -L "$2" ] && return 1     # the job already has its own: leave it
  mkdir -p "$(dirname "$2")" && ln -sfn "$1" "$2"
}
pull() {  # tag: from the host's image store, under the same tag
  [ -n "$store" ] || return 1
  docker image inspect "$1" >/dev/null 2>&1 && return 0
  docker pull -q "$store/$1" >/dev/null 2>&1 && docker tag "$store/$1" "$1" && docker image rm "$store/$1" >/dev/null 2>&1
}
[ -f "$tc/PREFETCH.json" ] && echo "pool host prefetch: $(tr -d '\n ' < "$tc/PREFETCH.json")" >&2
for item in "$@"; do
  ok=false
  case $item in
    playwright) link "$tc/ms-playwright" "${RUNNER_TEMP:?}/ms-playwright" && ok=true ;;
    models)
      ok=true
      for d in parakeet-models nemotron-models voice-models diarization-fixtures; do
        link "$tc/$d" "${RUNNER_TEMP:?}/$d" || ok=false
      done ;;
    build-caches) link "$tc/whistle-cache" "$root/.whistle-cache" && link "$tc/sherpa-cache" "$root/.sherpa-cache" && ok=true ;;
    flatpak) link "$tc/flatpak-gnome50" "$root/.desktop-flatpak-cache" && ok=true ;;
    harness:*) tag=$("$root/tests/harness-image.sh" tag "${item#harness:}" 2>/dev/null) && pull "$tag" && ok=true ;;
    audio) tag=$("$root/tests/recording/audio-image.sh" tag 2>/dev/null) && pull "$tag" && ok=true ;;
    *) echo "pool-prefetch: unknown item $item" >&2 ;;
  esac
  echo "${item//:/-}=$ok"
done
exit 0
