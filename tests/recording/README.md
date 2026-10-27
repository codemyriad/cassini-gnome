# Recording validation

The strategy follows the `../gocassini` Nextcloud Talk harness: use generated
media, validate saved artifacts rather than UI success messages, and keep
repeatable browser tests separate from the larger meeting-service integration.
The browser protocol and source format are shared with Cassini for Mac.

## Layers

| Layer | Checks |
|---|---|
| Python unit/integration tests | Framing and partial reads, size bounds, invalid metadata, path traversal, sequence rejection, gap preservation, interruption recovery, Opus finalization and decoding |
| AudioWorklet tests | PCM clipping/interleaving, non-finite values, render timestamps, partial-tail flush |
| Native Linux E2E | Actual PulseAudio capture from distinct virtual microphone/desktop sources; decode saved Opus and check each channel's expected frequency and cross-channel leakage |
| Chromium E2E | Actual extension invocation, MAIN hook, isolated relay, service worker, Chrome Native Messaging, Python host; real peer connections with different tones; mute, replacement offsets, navigation interruption, and mixed-tab fallback |
| GTK E2E | Recording controls, validation, quit finalization, opening a session, playing and pausing a source |
| Nextcloud Talk | Optional two-guest real meeting against the Go Cassini harness, with Chromium-generated fake devices |

Tests must fail when a required dependency or capture path is unavailable.
The regular pytest suite skips GStreamer-only tests on machines without GI;
the recording CI job installs those dependencies and executes the real paths.

## Isolation

`run.sh` starts a disposable PulseAudio container with two null sinks and no
network, then creates a private D-Bus session, Xvfb display, XDG runtime/config/
data/cache directories, and browser profile. Chromium explicitly uses X11.
It does not connect to the Wayland desktop, real microphone/speakers, or the
user's browser profile. `xdotool` is used only inside that private Xvfb display
to invoke Chrome's actual extension action and obtain `activeTab` permission.
No extension APIs, page hooks, PCM messages, or native-host responses are mocked.

## Run

Install Node 24+, Docker, Python 3 with PyGObject, GStreamer base/good plugins,
Xvfb, xauth, xdotool, dbus-run-session, and ImageMagick (GTK screenshots).
For GTK tests also install the app's GTK/libadwaita build requirements.

```sh
npm ci --prefix tests/recording
tests/recording/node_modules/.bin/playwright install chromium
python3 -m pytest -q
node --test browser-extension/tests/worklet.test.cjs
tests/recording/run.sh /usr/bin/python3 tests/recording/native.py
tests/recording/run.sh node tests/recording/browser.mjs
meson setup _build
meson compile -C _build
tests/recording/run.sh /usr/bin/python3 tests/recording/gtk.py --build _build
```

The scripts retain generated audio, machine-readable assertions and screenshots
in `tests/recording/artifacts/` (ignored by git). These contain only test tones.
CI uploads them even after failures. Temporary browser/audio resources are
removed on exit; the cached test image may be reused.

## Nextcloud Talk

Reuse a separate project in the adjacent Go Cassini harness; do not reset an
existing developer stack. A core Talk stack is sufficient for two local peers:

```sh
PROJECT_NAME=cassini-gnome-recording NEXTCLOUD_HOST_PORT=28880 \
  ../gocassini/bin/cassini dev stack up --services core --recording-backend none
PROJECT_NAME=cassini-gnome-recording NEXTCLOUD_HOST_PORT=28880 \
  ../gocassini/bin/cassini dev room create --name 'Recording test'
CASSINI_TALK_ONLY=1 TALK_CALL_URL=http://127.0.0.1:28880/call/ROOM_TOKEN \
  tests/recording/run.sh node tests/recording/browser.mjs
```

Talk runs separately because Chromium's fake-media-device flag interferes with
`tabCapture`; the deterministic tab test uses generated Web Audio instead.
Use the harness's project-scoped teardown when finished.

## Shared contract adoption

The canonical Git snapshot is pinned in `browser-protocol/PINNED_COMMIT`.
Run `python3 build-aux/verify-browser-protocol.py` to check its Git object/tree,
upstream hashes and byte-identical extension. `pytest` and `meson test` run the
36-vector stdio conformance runner with isolated output and no viewer launch.
Enable the opt-in suite with `meson configure _build -Drecording_tests=true`,
then run `meson test -C _build --suite recording`.

Each `run.sh` invocation writes to a unique `artifacts/run-*` directory;
`artifacts/latest` points at the most recently started run. Browser evidence
includes host stderr, page/worker console output, and manifest/RMS diagnostics
on failure. The script resolves Node before changing XDG directories; the tested
Node version is pinned in `.node-version`. WAV header sizes are checked after
Stop, since an active writer can update the header during a polling read.
Continuity checks use an independent millisecond wall clock, never the manifest's
whole-second creation timestamp. The original sender's asynchronous retirement
is distinct from the continuously active remote/replacement source timelines.
