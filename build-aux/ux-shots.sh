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
build-aux/ux-boards.sh "$app" "$out"
CASSINI_SCREENSHOT_SCHEME=dark build-aux/ux-boards.sh "$app" "$out/dark"
python3 tests/ui/check-dark.py "$out/dark"

for pair in "fr:fr" "pt_BR:pt-BR"; do
  locale_code=${pair%%:*}
  lang_tag=${pair#*:}
  CASSINI_SCREENSHOT_LANGUAGE="$locale_code" build-aux/ux-screenshot.sh "$app" "$out/first-run-$lang_tag.png" 2>"$out/first-run-$lang_tag.log"
  python3 tests/ui/check-localization.py "$out/first-run-$lang_tag.png" "$lang_tag"
  CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_LANGUAGE="$locale_code" build-aux/ux-screenshot.sh "$app" "$out/dark/first-run-$lang_tag.png" 2>"$out/dark/first-run-$lang_tag.log"
  python3 tests/ui/check-localization.py "$out/dark/first-run-$lang_tag.png" "$lang_tag"
done

CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/library.py "$out/gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/meeting.py "$out/meeting-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/interjections.py "$out/interjections.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/interjections.py "$out/dark/interjections.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/trash.py "$out/trash.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/trash.py "$out/dark/trash.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/speakers.py "$out/speakers-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/wide-speakers.py "$out/wide-speakers.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/wide-speakers.py "$out/dark/wide-speakers.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/listening-rows.py "$out/listening-rows.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voices.py "$out/voices-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voice-preparation.py "$out/voice-preparation.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voice-preparation.py "$out/dark/voice-preparation.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voice-models.py "$out/voice-models.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voices.py "$out/dark/voices-gui.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/voice-models.py "$out/dark/voice-models.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/speakers.py "$out/dark/speakers-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/build-refresh.py "$out/build-refresh.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/quit.py "$out/quit-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/background.py "$out/background.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/background.py "$out/dark/background.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/refresh.py "$out/refresh-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/recording.py "$out/recording-gui.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/recording.py "$out/dark/recording-gui.png"
G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram.py "$out/spectrogram-components.png"
CASSINI_SCREENSHOT_SCHEME=dark G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram.py "$out/dark/spectrogram-components.png"
G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram-live.py "$out/spectrogram-live.png"
G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram-browser.py "$out/spectrogram-browser.png"
CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram.py "$out/spectrogram-components-2x.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram.py "$out/dark/spectrogram-components-2x.png"
CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram-live.py "$out/spectrogram-live-2x.png"
CASSINI_SCREENSHOT_SCALE=2 G_DEBUG=fatal-warnings CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/spectrogram-browser.py "$out/spectrogram-browser-2x.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/accessible-buttons.py "$out/accessible-buttons.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/import-export.py "$out/import-export-gui.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/import-export.py "$out/dark/import-export-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/batch-import.py "$out/batch-import-gui.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/batch-import.py "$out/dark/batch-import-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/rename.py "$out/rename.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/rename.py "$out/dark/rename.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/search.py "$out/search-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/model-notices.py "$out/notices-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/settings.py "$out/settings-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/test-recording.py "$out/test-recording.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/companion-click.py "$out/companion-click.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/companion.py "$out/companion-setup.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/companion-health.py "$out/companion-health.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/recording-details.py "$out/recording-details.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/journey-parity.py "$out/journey-parity.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/journey-parity.py "$out/dark/journey-parity.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/companion-sandbox.py "$out/companion-sandbox.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/what-is-recorded.py "$out/what-is-recorded.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/library-move.py "$out/library-move-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/library-publication.py "$out/library-publication-gui.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/speech-models.py "$out/speech-models.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/speech-models.py "$out/dark/speech-models.png"
CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/diarization-models.py "$out/diarization-models.png"
CASSINI_SCREENSHOT_SCHEME=dark CASSINI_TEST_BUILD="$build" build-aux/ux-screenshot.sh tests/ui/diarization-models.py "$out/dark/diarization-models.png"

python3 build-aux/ci/ux_shots_manifest.py "$out"
