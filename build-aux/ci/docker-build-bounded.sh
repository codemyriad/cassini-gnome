#!/usr/bin/env bash
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
# Network acquisition deadlines do not bound a complete multi-index image build.
set -euo pipefail
if [ "$#" -lt 4 ] || [ "${2:-}" != docker ] || [ "${3:-}" != build ]; then
  echo "Usage: docker-build-bounded.sh NAME docker build ARG..." >&2
  exit 2
fi
name=$1
shift 3
limit=${CASSINI_HARNESS_BUILD_TIMEOUT:-180}
if [[ ! $limit =~ ^[0-9]{1,3}$ ]] || (( 10#$limit < 1 || 10#$limit > 300 )); then
  echo "CASSINI_HARNESS_BUILD_TIMEOUT must be whole seconds from 1 to 300" >&2
  exit 2
fi
limit=$((10#$limit))
workspace=$(mktemp -d /tmp/cassini-build-XXXXXXXX)
trap 'rm -rf "$workspace"' EXIT
arguments=()
for argument in "$@"; do
  # Keep the image-ID-only stdout contract without suppressing APT diagnostics.
  case "$argument" in -q|--quiet) ;; *) arguments+=("$argument") ;; esac
done
echo "Harness build [$name]: wall-clock budget ${limit}s (+10s kill grace)" >&2
if timeout --kill-after=10 "$limit" docker build --progress=plain --iidfile "$workspace/image" "${arguments[@]}" >&2; then
  image=$(cat "$workspace/image")
  [[ $image =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Harness build [$name] returned no valid image ID" >&2; exit 1; }
  printf '%s\n' "$image"
else
  status=$?
  if [ "$status" = 124 ] || [ "$status" = 137 ]; then
    echo "Harness build [$name] exceeded its ${limit}s wall-clock budget (exit $status); tests were not run." >&2
  else
    echo "Harness build [$name] failed (exit $status); tests were not run." >&2
  fi
  exit "$status"
fi
