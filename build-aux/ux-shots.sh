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
# flow SCRIPT OUTPUT [VAR=VALUE...]: a UI script against the build, into $out/OUTPUT.
flow() {
  local script=$1 output=$2
  shift 2
  job "$output" "$* CASSINI_TEST_BUILD=\"\$build\" build-aux/ux-screenshot.sh $script \"\$out/$output\""
}

job boards 'build-aux/ux-boards.sh "$app" "$out"'
job dark/boards 'CASSINI_SCREENSHOT_SCHEME=dark build-aux/ux-boards.sh "$app" "$out/dark"
python3 tests/ui/check-dark.py "$out/dark"'
flow tests/ui/test-recording.py test-recording.png
flow tests/ui/quit.py quit-gui.png
flow tests/ui/meeting.py meeting-gui.png
flow tests/ui/recording-details.py recording-details.png
flow tests/ui/spectrogram-live.py spectrogram-live.png G_DEBUG=fatal-warnings
flow tests/ui/spectrogram-live.py spectrogram-live-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow tests/ui/companion-sandbox.py companion-sandbox.png
flow tests/ui/accessible-buttons.py accessible-buttons.png
flow tests/ui/spectrogram-browser.py spectrogram-browser.png G_DEBUG=fatal-warnings
flow tests/ui/spectrogram-browser.py spectrogram-browser-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow tests/ui/library.py gui.png
flow tests/ui/interjections.py interjections.png
flow tests/ui/interjections.py dark/interjections.png CASSINI_SCREENSHOT_SCHEME=dark
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
flow tests/ui/trash.py trash.png
flow tests/ui/trash.py dark/trash.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/speakers.py speakers-gui.png
flow tests/ui/wide-speakers.py wide-speakers.png
flow tests/ui/wide-speakers.py dark/wide-speakers.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/listening-rows.py listening-rows.png
flow tests/ui/voices.py voices-gui.png
flow tests/ui/voice-preparation.py voice-preparation.png
flow tests/ui/voice-preparation.py dark/voice-preparation.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/voice-models.py voice-models.png
flow tests/ui/saved-voices-w6.py saved-voices-w6.png
flow tests/ui/saved-voices-w6.py dark/saved-voices-w6.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/voices.py dark/voices-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/voice-models.py dark/voice-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/speakers.py dark/speakers-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/build-refresh.py build-refresh.png
flow tests/ui/background.py background.png
flow tests/ui/background.py dark/background.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/refresh.py refresh-gui.png
flow tests/ui/recording.py recording-gui.png
flow tests/ui/recording.py dark/recording-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/spectrogram.py spectrogram-components.png G_DEBUG=fatal-warnings
flow tests/ui/spectrogram.py dark/spectrogram-components.png CASSINI_SCREENSHOT_SCHEME=dark G_DEBUG=fatal-warnings
flow tests/ui/spectrogram.py spectrogram-components-2x.png CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow tests/ui/spectrogram.py dark/spectrogram-components-2x.png CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings
flow tests/ui/import-export.py import-export-gui.png
flow tests/ui/import-export.py dark/import-export-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/batch-import.py batch-import-gui.png
flow tests/ui/batch-import.py dark/batch-import-gui.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/rename.py rename.png
flow tests/ui/rename.py dark/rename.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/search.py search-gui.png
flow tests/ui/starring.py starring.png
flow tests/ui/starring.py dark/starring.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/model-notices.py notices-gui.png
flow tests/ui/settings.py settings-gui.png
flow tests/ui/companion-click.py companion-click.png
flow tests/ui/companion.py companion-setup.png
flow tests/ui/companion-health.py companion-health.png
flow tests/ui/journey-parity.py journey-parity.png
flow tests/ui/journey-parity.py dark/journey-parity.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/what-is-recorded.py what-is-recorded.png
flow tests/ui/library-move.py library-move-gui.png
flow tests/ui/library-publication.py library-publication-gui.png
flow tests/ui/speech-models.py speech-models.png
flow tests/ui/speech-models.py dark/speech-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/diarization-models.py diarization-models.png
flow tests/ui/diarization-models.py dark/diarization-models.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/w1-token-states.py w1.png
flow tests/ui/w1-token-states.py dark/w1.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/w2-badges.py w2-badges.png
flow tests/ui/w2-badges.py dark/w2-badges.png CASSINI_SCREENSHOT_SCHEME=dark
flow tests/ui/w2-badges.py w2-badges-it.png CASSINI_SCREENSHOT_LANGUAGE=it
flow tests/ui/w2-badges.py dark/w2-badges-it.png CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_LANGUAGE=it

python3 build-aux/ci/parallel-jobs.py "$jobs"
python3 build-aux/ci/ux_shots_manifest.py "$out"
