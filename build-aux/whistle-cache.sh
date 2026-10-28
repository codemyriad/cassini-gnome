#!/bin/sh
# Download the pinned Whistle build inputs (weights, Needle engine and wrapper,
# Silero VAD, and the ONNX Runtime wheel for every pinned CPython ABI) into
# the cache that package builds read, checking every sha256.
#   build-aux/whistle-cache.sh            (cache: .whistle-cache)
#   CASSINI_WHISTLE_CACHE=/path build-aux/whistle-cache.sh
# Safe to run any number of times: files already there are only re-checked.
# Builds themselves never download (meson runs fetch.py --offline).
set -eu
cd "$(dirname "$0")/.."
exec python3 whistle/fetch.py --download-only --cache "${CASSINI_WHISTLE_CACHE:-.whistle-cache}"
