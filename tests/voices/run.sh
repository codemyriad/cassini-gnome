#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=$(realpath "$1")
models=$(realpath "$2")
audio=$(realpath "$3")
output=$(realpath -m "$4")
stage=$(realpath "$5")
modules=$(dirname "$bundle")/cassini_talk
sherpa_target=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sherpa"])' "$modules/paths.json")
ort_target=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["onnxruntime"])' "$modules/paths.json")
test -n "$ort_target"
extra=()
model_access=ro
if [ "$#" -ge 7 ]; then
  extra=(-v "$(realpath "$6"):/nemotron:rw" -v "$(realpath "$7"):/parakeet:ro" -e CASSINI_VOICE_QUEUE=1)
  model_access=rw
fi
test -n "$sherpa_target"
mkdir -p "$output"
# The harness image (tests/harness-image.sh): built when its tag is new, or loaded from CI's cache.
# Its base is pinned to one digest (here, or for the whole job by CI's cache key)
# that both its tag and its build name.
export CASSINI_HARNESS_BASES
CASSINI_HARNESS_BASES=$("$root/tests/harness-image.sh" pin whistle)
base=$("$root/tests/harness-image.sh" base whistle)
harness=$("$root/tests/harness-image.sh" tag whistle)
if ! docker image inspect "$harness" >/dev/null 2>&1; then
  docker build --pull -q --target harness --build-arg BASE="$base" -t "$harness" - < "$root/tests/whistle/Containerfile"
  "$root/tests/harness-image.sh" prune whistle
fi
# Every run: the cached harness brought up to the current archive (D68).
image=$(docker build --no-cache -q --build-arg HARNESS="$harness" - < "$root/tests/whistle/Containerfile")
trap 'docker image rm "$image" >/dev/null 2>&1 || true' EXIT
# timeout must be the container's PID 1: refuse an ENTRYPOINT inherited from the harness image.
entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$image")
[ "$entrypoint" = null ] || { echo "$image has ENTRYPOINT $entrypoint: timeout must be PID 1" >&2; exit 2; }
docker run --rm --network none --cpus=2 --memory=2g --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e CASSINI_WHISTLE_NETWORK_NONE=1 \
  -e XDG_CACHE_HOME=/out/cache -e PYTHONPATH=/modules -e CASSINI_SHERPA_BUNDLE="$sherpa_target" \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_talk:ro" \
  -v "$stage$sherpa_target:$sherpa_target:ro" -v "$stage$ort_target:$ort_target:ro" -v "$models:/models:$model_access" "${extra[@]}" \
  -v "$audio:/input.wav:ro" -v "$output:/out:rw" -w /source \
  "$image" timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_voice*.py --basetemp=/out/unit-temp --junitxml=/out/voice-tests.xml; python3 tests/voices/pipeline_probe.py /bundle /models/campplus-voxceleb-16k/357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b "$CASSINI_SHERPA_BUNDLE" /input.wav /out/pipeline --modules /modules; if [ "${CASSINI_VOICE_QUEUE:-0}" = 1 ]; then python3 tests/diarization/queue_probe.py /bundle /nemotron /input.wav /out/queue-whistle --modules /modules --voices /models; python3 tests/diarization/queue_probe.py /bundle /nemotron /input.wav /out/queue-parakeet --modules /modules --voices /models --parakeet /parakeet; python3 tests/voices/backfill_probe.py /bundle /nemotron /models /input.wav /out/backfill --modules /modules; fi'
