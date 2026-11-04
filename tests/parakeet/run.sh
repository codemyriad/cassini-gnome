#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]*$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, at least 1" >&2; exit 2; }
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
docker build -q -t cassini-whistle-test - < "$root/tests/whistle/Containerfile"
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/modules \
  -e CASSINI_WHISTLE_NETWORK_NONE=1 \
  -v "$root:/source:ro" -v "$modules:/modules/cassini_talk:ro" \
  -v "$ort:$ort_target:ro" -v "$runtime:$runtime_target:ro" \
  -v "$model:/model:ro" -v "$bundle:/bundle:ro" -v "$output:/out:rw" -w /source \
  cassini-whistle-test timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; cc --version > /out/compiler.txt; python3 -m pytest -q -p no:cacheprovider tests/test_parakeet.py --junitxml=/out/parakeet-tests.xml; python3 tests/parakeet/pipeline_probe.py --bundle /bundle --model /model --runtime "$1" --modules /modules --output /out/parakeet-linux.json; python3 tests/parakeet/queue_probe.py --bundle /bundle --model /model --runtime "$1" --modules /modules --output /out/parakeet-queue.json' \
  cassini-parakeet-test "$runtime_target"
