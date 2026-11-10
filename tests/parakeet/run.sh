#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
if [[ ${1:-} == --installed ]]; then
  datadir=$(realpath "$2")
  model=$(realpath "$3")
  output=$(realpath -m "$4")
  mkdir -p "$output"
  runtime=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sherpa"])' "$datadir/cassini_talk/paths.json")
  export CASSINI_WHISTLE_NETWORK_NONE=1 PYTHONDONTWRITEBYTECODE=1
  timeout --kill-after=30 "$limit" python3 "$root/tests/parakeet/pipeline_probe.py" --bundle "$datadir/whistle" --model "$model" --runtime "$runtime" --modules "$datadir" --output "$output/parakeet-linux.json"
  timeout --kill-after=30 "$limit" python3 "$root/tests/parakeet/queue_probe.py" --bundle "$datadir/whistle" --model "$model" --runtime "$runtime" --modules "$datadir" --output "$output/parakeet-queue.json"
  exit
fi
bundle=$(realpath "$1")
model=$(realpath "$2")
runtime=$(realpath "$3")
output=$(realpath -m "$4")
modules=$(dirname "$bundle")/cassini_talk
ort=$(dirname "$runtime")/python
ort_target=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["onnxruntime"])' "$modules/paths.json")
runtime_target=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sherpa"])' "$modules/paths.json")
test -n "$ort_target"
test -n "$runtime_target"
test -f "$modules/parakeet_client.py"
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
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/modules \
  -e CASSINI_WHISTLE_NETWORK_NONE=1 \
  -v "$root:/source:ro" -v "$modules:/modules/cassini_talk:ro" \
  -v "$ort:$ort_target:ro" -v "$runtime:$runtime_target:ro" \
  -v "$model:/model:ro" -v "$bundle:/bundle:ro" -v "$output:/out:rw" -w /source \
  "$image" timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; cc --version > /out/compiler.txt; python3 -m pytest -q -p no:cacheprovider tests/test_parakeet.py --junitxml=/out/parakeet-tests.xml; python3 tests/parakeet/pipeline_probe.py --bundle /bundle --model /model --runtime "$1" --modules /modules --output /out/parakeet-linux.json; python3 tests/parakeet/queue_probe.py --bundle /bundle --model /model --runtime "$1" --modules /modules --output /out/parakeet-queue.json' \
  cassini-parakeet-test "$runtime_target"
