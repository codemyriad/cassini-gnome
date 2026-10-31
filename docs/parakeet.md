# Parakeet Ultra engine (D16)

The optional Linux glibc x86_64 engine uses the pinned Cassini sherpa C API in a
fresh speech child for each job. GTK never loads sherpa. Whistle stays bundled
and remains the default. Both engines use the same FIR, Silero model and
SpeechGate segmentation; Parakeet starts ETA estimation at 0.3 times audio time.
Model acquisition and model selection are separate operations.

## Packaging handoff

D21 hands the sherpa additions in the package recipes to the GNOME agent.
Debian, RPM, Arch and Flatpak enable this engine with the same pinned inputs:

1. Fetch build inputs with `build-aux/sherpa-cache.sh`; set
   `CASSINI_SHERPA_CACHE` to the retained build cache. This fetches **only**
   `libsherpa-onnx-c-api.so` and `libonnxruntime.so`, checking D16's hashes and
   the pinned fork's `CASSINI-SHA256SUMS`. It never fetches speech weights.
2. Configure Meson with `-Dwhistle=enabled -Dsherpa=enabled` and absolute
   `-Dsherpa_cache=...` / `-Dwhistle_cache=...` cache directories. Configuration
   and installation are offline. The feature defaults to disabled.
3. Include the private `${libdir}/cassini-talk/sherpa/` tree. Keep both
   libraries byte for byte: **do not strip** them. Do not package the C++ wrapper.
   Include its manifest, pins, NOTICE and Apache-2.0 / MIT licence files.
4. Keep the existing private Python ONNX Runtime 1.31 wheel for Silero. The
   native sherpa runtime uses a separate pinned ONNX Runtime 1.27.1 library;
   these are intentionally distinct. No new system Python binding is needed.
5. Run `tests/parakeet/run.sh WHISTLE_BUNDLE MODEL_DIR SHERPA_DIR OUTPUT_DIR`
   against the installed tree. The package smoke checks use its `--installed`
   entry point inside the distribution or Flatpak sandbox with no network.
   Preparing test weights uses
   `tests/parakeet/prepare_model.py --cache DISK_BACKED_DIRECTORY`; this is a
   test fixture, never an installed package payload.

The exact four raw weight pins are in `parakeet_model.py`; runtime identities
and sizes are in `sherpa-pins.json`. The model is unmodified dist content,
CC-BY-4.0, approximately 443 MB compressed and 640 MB installed. Production
acquisition must use the existing dist-only D4 ModelStore, not the build fetcher.

The Flatpak grants network access for the explicit model Download action; the
D4 downloader still restricts requests and redirects to dist.gocassini.com.
Recognition remains offline, and package proofs explicitly unshare networking.

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

## Settings and queue policy

When the installed runtime is present, Preferences offers Download, retry,
Cancel and Remove, plus the Whistle / Parakeet radio group. It checks existing
weights in the background without requesting the manifest. Only Download makes
network requests; cancelling retains verified resumable bytes. Download completion
does not select Parakeet. The setting is read when each FIFO job starts, and
`transcript.model` records the actual engine, including after a failed/retried job.

Remove persists Whistle immediately, then waits off the UI thread for any model
load to finish before deleting weights. The loaded job completes in its child;
waiting jobs use Whistle. A failed raw-weight check deletes the bad revision and
uses the published fallback line. Missing runtime libraries do not delete valid
weights. App exit signals cancellation without joining network threads. A future
launch safely removes only recognized regular decode/metadata temporaries in the
staging tree before calculating download space; partial transfers still resume.

The GUI integration test drives the production controller and ModelStore with
small loopback fixture weights. Light and dark images show available, selected
and removed states. Unit barriers check selection while queued, Remove during
native loading and inference, and prompt exit during a stalled read. The offline
installed-worker proof additionally sends two recordings through the production
queue, checks both actual segment inputs against the golden before word timing,
checks track offsets, tags and capture-byte preservation, and requires distinct
reaped children with no engine imported in the parent.
