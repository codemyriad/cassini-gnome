#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: ux-gallery.sh BUILD_DIR OUTPUT_DIR [SCREEN...]
# D-1157 QA gallery: every main screen in light and dark, in the default (wide)
# window and a phone-width (narrow) one, plus Large Text (wide) for First Run,
# Library and Meeting. Files are gnome-NN-screen[-narrow|-large-text]-light|dark.png.
# Not run by CI; see docs/screenshots/GALLERY.md.
set -euo pipefail
[ "$#" -ge 2 ] || { echo "Usage: $0 build-dir output-dir [screen...]" >&2; exit 1; }
build=$(realpath "$1")
out=$(realpath -m "$2")
shift 2
cd "$(dirname "$(realpath "$0")")/.."
screens=("$@")
if [ "${#screens[@]}" -eq 0 ]; then
  screens=(library meeting search recording transcribing speakers preferences saved-voices recently-deleted
    export received first-run what-is-recorded meeting-menu main-menu library-row-menu recording-details
    keyboard-shortcuts help about rename set-date set-date-saved date-hover speaker-row background-voice background-folded
    background-opened)
fi
mkdir -p "$out"
jobs=$(mktemp /tmp/cassini-ux-gallery-jobs-XXXXXXXX)
trap 'rm -f "$jobs"' EXIT
export build out
job() {
  local screen=$1 size=$2 scheme=$3 large=$4
  local tag="$screen-$size$([ "$large" = 1 ] && echo -large-text)-$scheme"
  printf '%s\0%s\0' "$tag" "CASSINI_GALLERY_SCREEN=$screen CASSINI_GALLERY_SIZE=$size CASSINI_GALLERY_LARGE_TEXT=$large \
CASSINI_SCREENSHOT_SCHEME=$scheme CASSINI_TEST_BUILD=\"\$build\" build-aux/ux-screenshot.sh tests/ui/gallery.py \"\$out/$tag.json\"" >>"$jobs"
}
for screen in "${screens[@]}"; do
  for scheme in light dark; do
    job "$screen" wide "$scheme" 0
    job "$screen" narrow "$scheme" 0
    case "$screen" in
      first-run | library | meeting) job "$screen" wide "$scheme" 1 ;;
    esac
  done
done
python3 build-aux/ci/parallel-jobs.py "$jobs"
