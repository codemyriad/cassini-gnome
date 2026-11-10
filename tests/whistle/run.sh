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
    "$root/build-aux/ci/docker-build-bounded.sh" "whistle cached harness" docker build --pull -q --target harness --build-arg BASE="$base" -t "$harness" - < "$root/tests/whistle/Containerfile"
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
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source/src \
  -e CASSINI_WHISTLE_NETWORK_NONE=1 -e NEEDLE_TELEMETRY=unexpected -e DO_NOT_TRACK=unexpected \
  -v "$root:/source:ro" -v "$bundle:/bundle:ro" -v "$modules:/modules/cassini_talk:ro" -v "$ort:$ort_target:ro" -v "$output:/out:rw" -w /source \
  "$image" timeout --kill-after=30 "$limit" /bin/bash -c \
  'set -e; python3 -m pytest -q -p no:cacheprovider tests/test_whistle_spike.py --junitxml=/out/worker-tests.xml && PYTHONPATH=/modules python3 tests/whistle/probe.py /bundle /out/whistle-linux.json --modules /modules ; PYTHONPATH=/modules python3 tests/dc/probe.py /bundle /out/dc --modules /modules; golden_status=0; PYTHONPATH=/modules python3 tests/whistle/pipeline_probe.py /bundle /out/pipeline-linux.json --modules /modules || golden_status=$?; PYTHONPATH=/modules python3 tests/whistle/stream_probe.py /out; bash tests/whistle/ui-run.sh; exit "$golden_status"'
