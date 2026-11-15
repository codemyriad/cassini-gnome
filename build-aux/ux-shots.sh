#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: ux-shots.sh INSTALLED_APP BUILD_DIR OUTPUT_DIR
set -euo pipefail
[ "$#" -eq 3 ] || { echo "Usage: $0 installed-app build-dir output-dir" >&2; exit 1; }
app=$(realpath "$1")
build=$(realpath "$2")
out=$(realpath -m "$3")
cd "$(dirname "$(realpath "$0")")/.."

mkdir -p "$out" "$out/dark"
# D-1062: every run below has its own runtime directory, D-Bus session and Xvfb
# display, so they run on a few workers (CASSINI_PARALLEL_JOBS) instead of one
# after another; longest first. Each job's log prints whole when it ends.
jobs=$(mktemp /tmp/cassini-ux-shot-jobs-XXXXXXXX)
trap 'rm -f "$jobs"' EXIT
export app build out
job() { printf '%s\0%s\0' "$1" "$2" >>"$jobs"; }
# flow SCRIPT OUTPUT [VAR=VALUE...]: tests/ui/SCRIPT.py against the build, into $out/OUTPUT.
flow() {
  local script=$1 output=$2
  shift 2
  job "$output" "$* CASSINI_TEST_BUILD=\"\$build\" build-aux/ux-screenshot.sh tests/ui/$script.py \"\$out/$output\""
}

job boards 'build-aux/ux-boards.sh "$app" "$out"'
job dark/boards 'CASSINI_SCREENSHOT_SCHEME=dark build-aux/ux-boards.sh "$app" "$out/dark"
python3 tests/ui/check-dark.py "$out/dark"'
flow test-recording test-recording.png
flow quit quit-gui.png
flow meeting meeting-gui.png
flow recording-details recording-details.png
flow spectrogram-live spectrogram-live.png G_DEBUG=fatal-warnings
flow spectrogram-live spectrogram-live-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow companion-sandbox companion-sandbox.png
flow accessible-buttons accessible-buttons.png
flow spectrogram-browser spectrogram-browser.png G_DEBUG=fatal-warnings
flow spectrogram-browser spectrogram-browser-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow library gui.png
flow interjections interjections.png
flow interjections dark/interjections.png CASSINI_SCREENSHOT_SCHEME=dark
for pair in "fr:fr" "pt_BR:pt-BR"; do
  locale_code=${pair%%:*}
  lang_tag=${pair#*:}
  for folder in "" dark/; do
    scheme=
    [ -z "$folder" ] || scheme="CASSINI_SCREENSHOT_SCHEME=dark "
    job "${folder}first-run-$lang_tag" "${scheme}CASSINI_SCREENSHOT_LANGUAGE=$locale_code build-aux/ux-screenshot.sh \"\$app\" \"\$out/${folder}first-run-$lang_tag.png\" 2>\"\$out/${folder}first-run-$lang_tag.log\"
python3 tests/ui/check-localization.py \"\$out/${folder}first-run-$lang_tag.png\" $lang_tag"
  done
done
flow trash trash.png
flow trash dark/trash.png CASSINI_SCREENSHOT_SCHEME=dark
flow speakers speakers-gui.png
flow wide-speakers wide-speakers.png
flow wide-speakers dark/wide-speakers.png CASSINI_SCREENSHOT_SCHEME=dark
flow listening-rows listening-rows.png
flow voices voices-gui.png
flow voice-preparation voice-preparation.png
flow voice-preparation dark/voice-preparation.png CASSINI_SCREENSHOT_SCHEME=dark
flow voice-models voice-models.png
flow saved-voices-w6 saved-voices-w6.png
flow saved-voices-w6 dark/saved-voices-w6.png CASSINI_SCREENSHOT_SCHEME=dark
flow voices dark/voices-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow voice-models dark/voice-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow speakers dark/speakers-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow build-refresh build-refresh.png
flow background background.png
flow background dark/background.png CASSINI_SCREENSHOT_SCHEME=dark
flow refresh refresh-gui.png
flow recording recording-gui.png
flow recording dark/recording-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow spectrogram spectrogram-components.png G_DEBUG=fatal-warnings
flow spectrogram dark/spectrogram-components.png CASSINI_SCREENSHOT_SCHEME=dark G_DEBUG=fatal-warnings
flow spectrogram spectrogram-components-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow spectrogram dark/spectrogram-components-2x.png CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow import-export import-export-gui.png
flow import-export dark/import-export-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow batch-import batch-import-gui.png
flow batch-import dark/batch-import-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow rename rename.png
flow rename dark/rename.png CASSINI_SCREENSHOT_SCHEME=dark
flow search search-gui.png
flow starring starring.png
flow starring dark/starring.png CASSINI_SCREENSHOT_SCHEME=dark
flow model-notices notices-gui.png
flow settings settings-gui.png
flow companion-click companion-click.png
flow companion companion-setup.png
flow companion-health companion-health.png
flow journey-parity journey-parity.png
flow journey-parity dark/journey-parity.png CASSINI_SCREENSHOT_SCHEME=dark
flow what-is-recorded what-is-recorded.png
flow library-move library-move-gui.png
flow library-publication library-publication-gui.png
flow speech-models speech-models.png
flow speech-models dark/speech-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow diarization-models diarization-models.png
flow diarization-models dark/diarization-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow w1-token-states w1.png
flow w1-token-states dark/w1.png CASSINI_SCREENSHOT_SCHEME=dark
flow w2-badges w2-badges.png
flow w2-badges dark/w2-badges.png CASSINI_SCREENSHOT_SCHEME=dark
flow w2-badges w2-badges-it.png CASSINI_SCREENSHOT_LANGUAGE=it
flow w2-badges dark/w2-badges-it.png CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_LANGUAGE=it

python3 build-aux/ci/parallel-jobs.py "$jobs"
python3 build-aux/ci/ux_shots_manifest.py "$out"
