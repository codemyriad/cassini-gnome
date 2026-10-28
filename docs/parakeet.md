# Parakeet Ultra engine (D16)

The optional Linux glibc x86_64 engine uses the pinned Cassini sherpa C API in a
fresh speech child for each job. GTK never loads sherpa. Whistle stays bundled
and remains the default. Both engines use the same FIR, Silero model and
SpeechGate segmentation; Parakeet starts ETA estimation at 0.3 times audio time.
Model acquisition and model selection are separate operations.

## Packaging handoff

Packaging recipes are owned by the coordinator. To enable this engine:

1. Fetch build inputs with `build-aux/sherpa-cache.sh`; set
   `CASSINI_SHERPA_CACHE` to the retained build cache. This fetches **only**
   `libsherpa-onnx-c-api.so` and `libonnxruntime.so`, checking D16's hashes and
   the pinned fork's `CASSINI-SHA256SUMS`. It never fetches speech weights.
2. Configure Meson with `-Dwhistle=enabled -Dsherpa=enabled` and absolute
   `-Dsherpa_cache=...` / `-Dwhistle_cache=...` cache directories. Configuration
   and installation are offline. The feature defaults to disabled.
3. Include the private `${libdir}/cassini-viewer/sherpa/` tree. Keep both
   libraries byte for byte: **do not strip** them. Do not package the C++ wrapper.
   Include its manifest, pins, NOTICE and Apache-2.0 / MIT licence files.
4. Keep the existing private Python ONNX Runtime 1.31 wheel for Silero. The
   native sherpa runtime uses a separate pinned ONNX Runtime 1.27.1 library;
   these are intentionally distinct. No new system Python binding is needed.
5. Run `tests/parakeet/run.sh WHISTLE_BUNDLE MODEL_DIR SHERPA_DIR OUTPUT_DIR`
   against the installed tree. Preparing test weights uses
   `tests/parakeet/prepare_model.py --cache DISK_BACKED_DIRECTORY`; this is a
   test fixture, never an installed package payload.

The exact four raw weight pins are in `parakeet_model.py`; runtime identities
and sizes are in `sherpa-pins.json`. The model is unmodified dist content,
CC-BY-4.0, approximately 443 MB compressed and 640 MB installed. Production
acquisition must use the existing dist-only D4 ModelStore, not the build fetcher.

## Validation

`tests/test_parakeet.py` compares the size and every field offset of the ctypes
structures against a compiled C probe using the exact approved fork header.
The native proof requires a C compiler so this check cannot silently skip there.
The two coordinator goldens and their provenance are vendored under
`tests/parakeet/`. The checker verifies all segment input hashes and boundaries
before allowing D16's 80 ms word-boundary tolerance; only the specified trailing
ASCII punctuation variation is allowed. It does not use Whistle's text exception
or the coordinator's draft Parakeet exception file.

The proof runs with networking disabled, read-only inputs, private HOME and a
64 MiB tmpfs. It checks the installed modules, both native libraries, the private
Python ONNX Runtime, a new child PID per job and child reaping. Artifacts include
segments, words, input hashes, library maps and timing differences. On the local
installed build both goldens had eight segments and 66 words, exact input hashes,
exact text and zero word-boundary difference.
