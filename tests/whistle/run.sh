#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=$(realpath "$1")
output=$(realpath -m "$2")
modules=$(dirname "$bundle")/cassini_viewer
test -f "$modules/whistle_client.py"
# CI passes the stage explicitly; the default keeps the documented /usr layout.
stage=${3:-$(dirname "$(dirname "$(dirname "$(dirname "$bundle")")")")}
ort_target=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["onnxruntime"])' "$modules/paths.json")
test -n "$ort_target"
ort=$(realpath "$stage$ort_target")
test -d "$ort/onnxruntime"
mkdir -p "$output"
docker build -q -t cassini-whistle-test - < "$root/tests/whistle/Containerfile"
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source/src \
  -e CASSINI_WHISTLE_NETWORK_NONE=1 -e NEEDLE_TELEMETRY=unexpected -e DO_NOT_TRACK=unexpected \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_viewer:ro" -v "$ort:$ort_target:ro" -v "$output:/out:rw" -w /source \
  cassini-whistle-test /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_whistle_spike.py --junitxml=/out/worker-tests.xml && PYTHONPATH=/modules python3 tests/whistle/probe.py /bundle /out/whistle-linux.json --modules /modules ; PYTHONPATH=/modules python3 tests/dc/probe.py /bundle /out/dc --modules /modules; PYTHONPATH=/modules python3 tests/bleed/probe.py /bundle /out/bleed --modules /modules; golden_status=0; PYTHONPATH=/modules python3 tests/whistle/pipeline_probe.py /bundle /out/pipeline-linux.json --modules /modules || golden_status=$?; PYTHONPATH=/modules python3 tests/whistle/stream_probe.py /out; bash tests/whistle/ui-run.sh; exit "$golden_status"'
