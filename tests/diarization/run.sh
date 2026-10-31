#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
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
docker build -q -t cassini-whistle-test - < "$root/tests/whistle/Containerfile"
docker run --rm --network none --cpus=2 --memory=2g --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e CASSINI_WHISTLE_NETWORK_NONE=1 \
  -e XDG_CACHE_HOME=/out/cache -e PYTHONPATH=/modules \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_talk:ro" \
  -v "$stage$ort_target:$ort_target:ro" -v "$stage$sherpa_target:$sherpa_target:ro" \
  -v "$parakeet:/parakeet:ro" -v "$models:/models:rw" -v "$audio:/input.wav:ro" -v "$output:/out:rw" \
  -w /source cassini-whistle-test /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_diarization*.py --basetemp=/out/unit-temp --junitxml=/out/diarization-tests.xml; python3 tests/diarization/pipeline_probe.py /bundle /models/nemotron-3-diarization-int8/47c221ea9b4d4e7f6c108bd098e769bc706cdc986c29f6133c93cae335fe6779 /input.wav /out/pipeline --modules /modules; python3 tests/diarization/pipeline_probe.py /bundle /models/nemotron-3-diarization-int8/47c221ea9b4d4e7f6c108bd098e769bc706cdc986c29f6133c93cae335fe6779 /input.wav /out/parakeet --modules /modules --parakeet-model /parakeet; python3 tests/diarization/queue_probe.py /bundle /models /input.wav /out/queue --modules /modules'
