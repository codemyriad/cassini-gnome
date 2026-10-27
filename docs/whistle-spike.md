# Whistle bundling spike (W2-3 / S-8a)

The pinned Linux x86_64 engine recognises the shared 11-second JFK WAV in one
persistent child process. This is a packaging and offline-recognition spike;
there is no new transcription control in the app yet. UX13 will connect it.
The file-only worker accepts PCM16 mono 16 kHz WAVs up to 30 seconds.

## Exact build inputs

`whistle/pins.json` records full URLs, revisions and SHA256 values. Weights are
Whistle revision `ca5287601bef25af26dcf2e1b2bdc0843a7c19e5`; native engine is
Needle 3.3.0 revision `93e88ede867e4311c39151285e74df513e4b835b`. Its wheel
contains only `libneedle3.so`. The separate Apache-2.0 Python wrapper is the
3.2.0 wheel already used by Cassini Live. It imports directly from the wheel;
no runtime pip, Hugging Face client or model download is required.

Only the build script downloads anything. Corrupt downloads or existing bundles
fail closed. It writes a hash manifest for the three pinned artifacts and the
locally compiled CPU-count bridge, and retains source, pins and licence notices.
The bundle is about 20 MB, including 17 MB weights, and is glibc x86_64 only.

```sh
python3 whistle/fetch.py /tmp/whistle-bundle --cache /tmp/whistle-cache
meson setup _whistle-build whistle -Dbundle=/tmp/whistle-bundle --prefix=/usr
DESTDIR="$PWD/_whistle-stage" meson install -C _whistle-build
tests/whistle/run.sh "$PWD/_whistle-stage/usr/share/cassini-viewer/whistle" /tmp/whistle-results
```

The harness builds a small Ubuntu image, then runs with `--network none`, a
read-only filesystem, no capabilities, private temporary storage, and read-only
source/bundle mounts. It has no display, audio socket, browser profile or device
mounts. The report asserts only loopback exists. It transcribes twice in one
child, compares byte-for-byte with Mac's published engine output and compares
normalised words with the shared reference. The reference differs in punctuation
(`. Ask` versus `, ask`); the original reference stays unchanged.

## Privacy and process boundaries

Before the wrapper import or actual engine/model constructor, the worker requires
`NEEDLE_TELEMETRY=0`, `DO_NOT_TRACK=1`, `HF_HUB_OFFLINE=1`, and
`ORT_DISABLE_TELEMETRY=1`. The client overrides inherited values. The guard test
compiles a native library whose constructor writes a marker: every missing or
wrong variable prevents the constructor from running. Correct values allow it.
Python networking is blocked as well; the real inference test has no network
interface beyond loopback, including for native code.

The pinned engine has no thread-count API. A small GPL CPU-count bridge affects
only the recognition child through `LD_PRELOAD`, defaulting to two CPUs. GTK is
never preloaded and never imports the engine. A future UI caller must run the
client's blocking file IO/requests on a worker thread. Replies are bounded and
time out even when the child sends a partial line; crashes and close reap the
child. Unit tests exercise two requests, partial-line timeout and crash cleanup.

## Packaging handoff to the coordinator

No existing packaging manifest is changed by this spike. The separate
`whistle/meson.build` verifies and installs the bundle under
`share/cassini-viewer/whistle` and the two Python modules under the existing
`share/cassini-viewer/cassini_viewer` module directory.

For a later packaging PR: supply the three immutable build artifacts and their
hashes from `pins.json` through each packaging system's build sources/cache;
run `fetch.py` at build preparation (never at app runtime), then run this separate
Meson install into the same staging root. The native CPU bridge needs a C compiler
and libc development headers at build time, with glibc/libstdc++ at runtime.
For Flatpak, declare immutable build sources and prepare the cache before the
network-disabled build phase. Ship `NOTICE.md`, the Apache licence and the bridge
source with the bundle. Enable shipping only for glibc x86_64 until another
pinned engine is independently tested. No package dependency or launcher change
is necessary merely to run this spike; app integration remains UX13.
