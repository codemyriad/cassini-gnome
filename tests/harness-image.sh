#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# The cacheable stage of a test harness image: the Ubuntu base, the harness's
# own packages and the app's static runtime Depends from debian/control. The
# app's package is never in it: each run installs app.deb on top, and apt
# resolves its dependencies there, every run.
#
#   tests/harness-image.sh tag NAME     the image tag for harness NAME
#   tests/harness-image.sh depends      debian/control's static Depends, for apt-get satisfy
#   tests/harness-image.sh prune NAME   remove NAME's harness images other than the current tag
#
# The tag hashes every input of the harness stage (its Dockerfile, debian/control,
# a non-default base, this script) and ends with the epoch, the ISO week unless
# CASSINI_HARNESS_EPOCH says otherwise. A new week or any changed input names a
# new image, built with --pull, so a harness is never older than a week and never
# outlives the file that defines it. CI caches these images (saved from main only).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
epoch=${CASSINI_HARNESS_EPOCH:-$(date -u +%G-W%V)}
[[ $epoch =~ ^[0-9A-Za-z-]{1,32}$ ]] || { echo "CASSINI_HARNESS_EPOCH must be 1 to 32 letters, digits or dashes" >&2; exit 2; }

inputs() {
  case "$1" in
    desktop) echo tests/desktop/Dockerfile debian/control ;;
    build-refresh) echo tests/build-refresh/Dockerfile debian/control ;;
    browser-setup) echo tests/browser-setup/Dockerfile debian/control ;;
    whistle) echo tests/whistle/Containerfile ;;
    *) echo "unknown harness: $1" >&2; return 2 ;;
  esac
}

case "${1:-}" in
  tag)
    list=$(inputs "${2:?harness name}")
    read -ra files <<<"$list"
    base=
    if [ "$2" = browser-setup ]; then base=${CASSINI_BROWSER_TEST_BASE:-}; fi
    hash=$(cd "$root" && { cat "${files[@]}" tests/harness-image.sh; printf 'base=%s\n' "$base"; } | sha256sum | cut -c1-16)
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
    echo "usage: $0 tag NAME | depends | prune NAME" >&2
    exit 2
    ;;
esac
