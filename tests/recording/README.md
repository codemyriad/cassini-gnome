# Recording validation

The strategy follows the `../gocassini` Nextcloud Talk harness: use generated
media, validate saved artifacts rather than UI success messages, and keep
repeatable browser tests separate from the larger meeting-service integration.
The browser protocol and source format are shared with Cassini for Mac.

## Layers

`app-control.mjs` also drives the production What is recorded dialog through
off/on choices, then reloads a tab and terminates/restarts its worker. It checks
frozen ended WAVs, new spans with the same source identity, continuous microphone,
actual system fallback tones, independent concurrent capture, the hidden info
line and no duplicate folders or automatic switch back. `sources.py` separately
proves native stream release/restart and asynchronous Stop during a source change.

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
shared stdio conformance runner with isolated output and no viewer launch.
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

## App-driven browser recording

`CASSINI_TEST_BUILD=$PWD/_build tests/recording/run.sh node tests/recording/app-control.mjs`
runs the real extension, production native audio/control hosts, private broker and actual
Library widgets. It records two generated WebRTC calls while both tabs and the
private output sink are muted, checks separate remote tones and native microphone
copies, and proves that a later call does not join manual Record. The meeting
page stops one while the other grows; the pill and actual private portal shortcut
stop all. Automatic recording continues with no visible window, and the native
GNotification Open/Stop actions travel over private D-Bus. A new call starts while
the app is hidden. All device, framing, IPC and widget code is production code;
only the meeting peers and private desktop services are fixtures.

`tests/recording/run.sh /usr/bin/python3 tests/recording/browser-microphone.py`
checks two independent microphone copies, a per-session mic-off choice, tone and
bleed thresholds, stopping one copy and keeping the other growing. Hosted
Recording runs this in its own private Pulse container. The app-control muted-sink
proof also requires `pactl` (`pulseaudio-utils`), always given the explicit private
socket. It refuses the default desktop server.
`CASSINI_TEST_EXTENSION` can select an exact upstream checkout for cross-app PR review.

## Readiness and native repeat validation (R3)

Startup sequencing uses bounded conditions rather than elapsed sleeps. Native
capture first observes one second of continuously nonzero PCM from **each actual
private PulseAudio monitor**, then counts 2.5 seconds of complete interleaved PCM
at the recorder. GTK waits for painted/allocated windows, 0.4 seconds of captured
PCM before Quit, finalization and advancing playback. Browser/Talk checks wait
for real UI/state transitions and captured samples. Poll intervals are not
startup assumptions. The 35-second worker-idle exercise and three-second host
stall remain intentional lifecycle/fault-injection durations.

The final native assertions remain unchanged: two channels, at least two seconds
of decoded audio, expected tones above 0.08, cross-channel tones below 0.01, and
the same measurement window. Browser/Talk duration/tone/bleed assertions are also
unchanged. A timeout fails with observed progress; it does not shorten the
required recording or turn an assertion into a retry. Native `results.json`
checkpoints retain the current stage, failure traceback, producer/capture counts, decoded frames and measured
amplitudes when available, alongside the original Opus/WAV files. A 60-second
watchdog bounds synchronous C-level stalls and preserves thread traces in
`threads.log`. Observations are detached before EOS/teardown so Python streaming
callbacks cannot obstruct synchronous shutdown. GTK retains
failure state and a screenshot. Browser failure diagnostics remain in place.

For repeated validation, run the native command several times through `run.sh`;
CI runs three native invocations without retrying failures;
each invocation creates a new private audio container/display and unique evidence
directory. Report every result, including failures, rather than retrying until a
single success. The readiness unit tests use finite synthetic buffers and verify
silent-input rejection and bounded timeout diagnostics without any audio server.


Failure evidence is collected before a recorder tears down its pipeline. The
`gst-evidence.json` file keeps the error domain/code, exact source path and full
GStreamer debug string, plus a pipeline graph and negotiated source caps/clock.
An appender exception is recorded separately with its track, offset and frame
count, so a later generic “Internal data stream error” cannot conceal the cause.
The comfort proof fails immediately on such an error instead of timing out with
zero levels. Native checkpoints also precede EOS/failure cleanup, so a blocked
C-level state change retains the last graph and the existing thread dump.

Every run retains per-process `gstreamer-<pid>.log`, `pulse-server.log` and
`pulse-state.log` from its private container. The
[GStreamer runtime controls](https://gstreamer.freedesktop.org/documentation/gstreamer/running.html)
document the `%p` log-file pattern; it prevents helper processes from overwriting
one another's evidence. No readiness, duration, tone, bleed or disk threshold is
relaxed, and failures are not retried into a passing result.

The GTK recording probe uses the same passive message/appender fault hooks as the
native and comfort probes. A GUI capture failure therefore retains the original
append exception, track/offset/frame count, complete bus error and pre-disposal
pipeline evidence in gst-evidence.json, in addition to results.json and raw GST
logs. This also covers failures at the first native capture, before the comfort
probe has run. No readiness, duration, tone or bleed thresholds differ.

The standalone browser microphone proof uses the same 100 ms spectral-magnitude
windows as `signal.mjs`, over its original final 12,000 mono frames (250 ms).
Pulse clock corrections can shift phase inside a continuous tone: summing one
complex coefficient across the whole interval can cancel real audio. A hosted
failure had RMS 0.17675 (the expected 0.25-amplitude sine) but coherent power
0.000555; windowed magnitude gives 0.008003. Both are in the original squared
amplitude units. The wanted-power threshold stays 0.001, bleed stays below 1%,
and every frame-count/readiness/duration requirement is unchanged. Analytic
controls still reject silence, another source, a short 20%-duty burst and real
cross-source bleed. Failure JSON retains RMS, magnitude, power and measured
frame count alongside the WAVs; this change never touches captured audio.
