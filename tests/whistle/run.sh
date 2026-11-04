#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# E1: bound the run so a hang fails instead of living for days (exit 124/137).
limit=${CASSINI_RUN_TIMEOUT:-1200}
[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=$(realpath "$1")
output=$(realpath -m "$2")
modules=$(dirname "$bundle")/cassini_talk
test -f "$modules/whistle_client.py"
# CI passes the stage explicitly; the default keeps the documented /usr layout.
stage=${3:-$(dirname "$(dirname "$(dirname "$(dirname "$bundle")")")")}
ort_target=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["onnxruntime"])' "$modules/paths.json")
test -n "$ort_target"
ort=$(realpath "$stage$ort_target")
test -d "$ort/onnxruntime"
mkdir -p "$output"
# The harness image (tests/harness-image.sh): built when its tag is new, or loaded from CI's cache.
harness=$("$root/tests/harness-image.sh" tag whistle)
if ! docker image inspect "$harness" >/dev/null 2>&1; then
  docker build --pull -q -t "$harness" - < "$root/tests/whistle/Containerfile"
  "$root/tests/harness-image.sh" prune whistle
fi
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source/src \
  -e CASSINI_WHISTLE_NETWORK_NONE=1 -e NEEDLE_TELEMETRY=unexpected -e DO_NOT_TRACK=unexpected \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_talk:ro" -v "$ort:$ort_target:ro" -v "$output:/out:rw" -w /source \
  "$harness" timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_whistle_spike.py --junitxml=/out/worker-tests.xml && PYTHONPATH=/modules python3 tests/whistle/probe.py /bundle /out/whistle-linux.json --modules /modules ; PYTHONPATH=/modules python3 tests/dc/probe.py /bundle /out/dc --modules /modules; golden_status=0; PYTHONPATH=/modules python3 tests/whistle/pipeline_probe.py /bundle /out/pipeline-linux.json --modules /modules || golden_status=$?; PYTHONPATH=/modules python3 tests/whistle/stream_probe.py /out; bash tests/whistle/ui-run.sh; exit "$golden_status"'
