#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# The cacheable stage of a test harness image: the Ubuntu base, the harness's
# own packages and the app's static runtime Depends from debian/control. The
# app's package is never in it. Every run first brings it up to the current
# archive (apt-get dist-upgrade, D68: the cache saves time, never freshness), then
# installs app.deb on top, so apt resolves its dependencies there, every run.
#
#   tests/harness-image.sh pin NAME...  CASSINI_HARNESS_BASES, with every NAME's base pinned
#   tests/harness-image.sh base NAME    NAME's base image at its digest, for --build-arg BASE
#   tests/harness-image.sh tag NAME     the image tag for harness NAME
#   tests/harness-image.sh depends      debian/control's static Depends, for apt-get satisfy
#   tests/harness-image.sh prune NAME   remove NAME's harness images other than the current tag
#
# The harness stage is built FROM ${BASE}: the image its Dockerfile's ARG BASE
# names (CASSINI_BROWSER_TEST_BASE for browser-setup), pinned to the digest of
# this platform's manifest. CASSINI_HARNESS_BASES holds the pins, IMAGE@sha256:...
# separated by spaces, at most one per image; an image it lacks is resolved from
# the registry (one GET of its index: one Docker Hub pull), and a base that
# cannot be resolved is an error, never an unpinned build. Pin once (run.sh, or
# CI's cache key for the whole job) and reuse it: the tag and the build then name
# the same base, so a cached harness and a fresh one are byte for byte the same
# base. A base named by digest (CASSINI_BROWSER_TEST_BASE=IMAGE@sha256:...) is
# immutable: it is resolved itself, never replaced by a pin of the same image.
# Setting CASSINI_HARNESS_BASES only skips the lookup (tests, or Docker Hub's rate
# limit); the build still pulls that digest and every run still runs apt-get.
# A digest: `docker buildx imagetools inspect --raw IMAGE`, the linux/ARCH entry.
#
# The tag hashes every input of the harness stage (its Dockerfile, debian/control,
# the pinned base, this script) and ends with the epoch, the ISO week unless
# CASSINI_HARNESS_EPOCH says otherwise. A new week, a moved base or any changed
# input names a new image, built with --pull, so a harness is never older than a
# week or its base, and never outlives the file that defines it. CI caches these
# images (saved from main only).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
epoch=${CASSINI_HARNESS_EPOCH:-$(date -u +%G-W%V)}
[[ $epoch =~ ^[0-9A-Za-z-]{1,32}$ ]] || { echo "CASSINI_HARNESS_EPOCH must be 1 to 32 letters, digits or dashes" >&2; exit 2; }

read -ra pinned <<<"${CASSINI_HARNESS_BASES:-}"
for pin in "${pinned[@]}"; do
  [[ $pin =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] || { echo "CASSINI_HARNESS_BASES: $pin is not IMAGE@sha256:<64 hex digits>" >&2; exit 2; }
done
pinned_names=$(for pin in "${pinned[@]}"; do echo "${pin%@sha256:*}"; done | sort | uniq -d)
[ -z "$pinned_names" ] || { echo "CASSINI_HARNESS_BASES pins $pinned_names more than once" >&2; exit 2; }

dockerfile() {
  case "$1" in
    desktop | build-refresh | browser-setup) echo "tests/$1/Dockerfile" ;;
    whistle) echo tests/whistle/Containerfile ;;
    *) echo "unknown harness: $1" >&2; return 2 ;;
  esac
}

inputs() {
  dockerfile "$1"
  if [ "$1" != whistle ]; then echo debian/control; fi
}

# The image the harness stage is built from, as its Dockerfile's ARG BASE names it.
image() {
  local file default
  file=$(dockerfile "$1") || return
  default=$(sed -n 's/^ARG BASE=\([^[:space:]]\{1,\}\)$/\1/p' "$root/$file")
  [ "$(printf '%s\n' "$default" | wc -w)" = 1 ] || { echo "$file must name its base once, as ARG BASE=IMAGE" >&2; return 2; }
  if [ "$1" = browser-setup ] && [ -n "${CASSINI_BROWSER_TEST_BASE:-}" ]; then default=$CASSINI_BROWSER_TEST_BASE; fi
  echo "$default"
}

# IMAGE@DIGEST: the pin CASSINI_HARNESS_BASES holds for the image, or else the
# digest of the manifest the registry serves this Docker host's platform. A
# reference that names its own digest is resolved itself, never by a pin.
base() {
  local reference name pin platform digest
  reference=$(image "$1") || return
  name=${reference%@*}
  if [ "$reference" = "$name" ]; then
    for pin in "${pinned[@]}"; do
      if [ "${pin%@sha256:*}" = "$name" ]; then
        echo "$pin"
        return
      fi
    done
  fi
  platform=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}') || { echo "cannot ask Docker for its platform" >&2; return 1; }
  # One GET of the raw manifest (a formatted inspect fetches every platform's too).
  # A multi-platform index: the one manifest for this platform. A single manifest:
  # itself, whose digest is that of the raw bytes.
  digest=$(docker buildx imagetools inspect --raw "$reference" | python3 -I -c '
import hashlib, json, sys
raw = sys.stdin.buffer.read()
try:
    manifest = json.loads(raw)
except ValueError:
    sys.exit("no manifest")
if isinstance(manifest, dict) and "manifests" in manifest:
    found = [m["digest"] for m in manifest["manifests"]
             if "{}/{}".format(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture")) == sys.argv[1]]
else:
    found = ["sha256:" + hashlib.sha256(raw).hexdigest()]
if len(found) != 1:
    sys.exit(f"{len(found)} manifests for {sys.argv[1]}")
print(found[0])
' "$platform") || digest=
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "cannot resolve the $platform digest of $reference; to skip the registry lookup, set CASSINI_HARNESS_BASES=$name@sha256:..." >&2
    return 1
  }
  echo "$name@$digest"
}

case "${1:-}" in
  pin)
    shift
    pins=("${pinned[@]}")
    for name in "$@"; do
      pin=$(base "$name")
      # A base named by its own digest needs no pin, and must not pin its image for the others.
      reference=$(image "$name")
      if [ "$reference" != "${reference%@*}" ]; then continue; fi
      # Pinned from here on: a second NAME on the same image never looks it up again.
      if [[ " ${pins[*]} " != *" $pin "* ]]; then pins+=("$pin") pinned+=("$pin"); fi
    done
    echo "${pins[*]}"
    ;;
  base)
    base "${2:?harness name}"
    ;;
  tag)
    list=$(inputs "${2:?harness name}")
    mapfile -t files <<<"$list"
    pin=$(base "$2")
    hash=$(cd "$root" && { cat "${files[@]}" tests/harness-image.sh; printf 'base=%s\n' "$pin"; } | sha256sum | cut -c1-16)
    echo "cassini-harness-$2:$hash-$epoch"
    ;;
  depends)
    # The cassini-talk stanza's Depends, continuation lines joined, without the
    # ${...} substitution variables only the package build can fill in.
    awk '/^Package:/ { pkg = ($2 == "cassini-talk") }
         /^[^ \t]/ { field = 0 }
         pkg && /^Depends:/ { field = 1; sub(/^Depends:/, "") }
         field { printf "%s ", $0 }' "$root/debian/control" |
      sed -E 's/\$\{[^}]*\}[[:space:]]*,?//g; s/[[:space:]]+/ /g; s/^ //; s/,? *$//'
    echo
    ;;
  prune)
    current=$("$0" tag "${2:?harness name}")
    docker image ls --format '{{.Repository}}:{{.Tag}}' "cassini-harness-$2" |
      { grep -vxF "$current" || true; } | xargs -r docker image rm >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 pin NAME... | base NAME | tag NAME | depends | prune NAME" >&2
    exit 2
    ;;
esac
