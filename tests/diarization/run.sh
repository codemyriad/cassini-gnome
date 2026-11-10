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
parakeet=$(realpath "$6")
modules=$(dirname "$bundle")/cassini_talk
ort_target=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["onnxruntime"])' "$modules/paths.json")
sherpa_target=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sherpa"])' "$modules/paths.json")
test -n "$ort_target" && test -n "$sherpa_target"
mkdir -p "$output"
# CI shares one current-archive image across all four offline steps. A
# standalone invocation still refreshes once and owns only that fresh image.
if [ -n "${CASSINI_OFFLINE_HARNESS_IMAGE:-}" ]; then
  image=$("$root/tests/whistle/refresh-image.sh" check "$CASSINI_OFFLINE_HARNESS_IMAGE" "${CASSINI_OFFLINE_HARNESS_JOB:?}")
else
  # The harness image (tests/harness-image.sh): built when its tag is new, or loaded from CI's cache.
  # Its base is pinned to one digest (here, or for the whole job by CI's cache key)
  # that both its tag and its build name.
  export CASSINI_HARNESS_BASES
  CASSINI_HARNESS_BASES=$("$root/tests/harness-image.sh" pin whistle)
  base=$("$root/tests/harness-image.sh" base whistle)
  harness=$("$root/tests/harness-image.sh" tag whistle)
  if ! docker image inspect "$harness" >/dev/null 2>&1; then
    docker build --pull -q --target harness --build-arg BASE="$base" -t "$harness" - < "$root/tests/whistle/Containerfile"
  fi
  offline_job=standalone-$$
  image=$("$root/tests/whistle/refresh-image.sh" build "$harness" "$offline_job")
  trap '"$root/tests/whistle/refresh-image.sh" remove "$image" "$offline_job" >/dev/null 2>&1 || true' EXIT
fi
# timeout must be the container's PID 1: refuse an ENTRYPOINT inherited from the harness image.
entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$image")
[ "$entrypoint" = null ] || { echo "$image has ENTRYPOINT $entrypoint: timeout must be PID 1" >&2; exit 2; }
docker run --rm --network none --cpus=2 --memory=2g --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e CASSINI_WHISTLE_NETWORK_NONE=1 \
  -e XDG_CACHE_HOME=/out/cache -e PYTHONPATH=/modules \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_talk:ro" \
  -v "$stage$ort_target:$ort_target:ro" -v "$stage$sherpa_target:$sherpa_target:ro" \
  -v "$parakeet:/parakeet:ro" -v "$models:/models:rw" -v "$audio:/input.wav:ro" -v "$output:/out:rw" \
  -w /source "$image" timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_diarization*.py --basetemp=/out/unit-temp --junitxml=/out/diarization-tests.xml; python3 tests/diarization/pipeline_probe.py /bundle /models/nemotron-3-diarization-int8/47c221ea9b4d4e7f6c108bd098e769bc706cdc986c29f6133c93cae335fe6779 /input.wav /out/pipeline --modules /modules; python3 tests/diarization/pipeline_probe.py /bundle /models/nemotron-3-diarization-int8/47c221ea9b4d4e7f6c108bd098e769bc706cdc986c29f6133c93cae335fe6779 /input.wav /out/parakeet --modules /modules --parakeet-model /parakeet; python3 tests/diarization/queue_probe.py /bundle /models /input.wav /out/queue --modules /modules'
