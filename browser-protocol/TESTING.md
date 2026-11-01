# Layered recording checks

1. Portable raw-JSON stdio vectors in conformance/, against both executable hosts. Includes
   null/type/lexical-number rules, nonfinite input, sequence, silence gaps, EOF, errors and WAV
   finalization. Swift additionally tests malformed/truncated/oversize native framing directly.
2. Portable extension worklet/clock/buffering unit tests (extension/tests/*.test.cjs).
3. Browser scenarios use pinned Playwright 1.61.1, Chromium 149.0.7827.55. The shared tone
   primitives are in scenarios/; Mac adapters are BrowserExtension/tests/*.mjs. Distinct
   440/880 Hz peers and 660 Hz replacement verify spectral separation (<1% power bleed),
   persisted-sample mute, shared timeline (<400 ms), sender detach/restore, 3-second host
   SIGSTOP, 35-second idle/explicit worker termination, iframe capture, navigation and real
   stereo tabCapture/offscreen cleanup. Fixed waits create the intentional idle/stall/mute
   intervals; readiness/finalization use observed states and recorded frame counts.
4. Native Swift sink uses injectable writer and synthetic AVAudioPCMBuffers: one hour ±50 ppm,
   per-source bounded stalls/tail silence, quantization/NaN, planar/interleaved 1/2/4 channels,
   format change, failure once and late callbacks. Loader tests include GNOME timestamps,
   corrupt/empty metadata, stale frame counts, and escaping symlinks.
5. Hardware-native validation is an explicit manual gate. On Mac run
   scripts/check-native-recording.sh after real system-audio permission is granted. The
   sink-only variant does not establish CoreAudio/TCC/device compatibility. Microphone testing
   needs an actual input device. CI builds the app but cannot grant privacy consent.

Hosted Mac CI runs the contract, native writer/loader and WebRTC/media scenarios on macos-15 /
Xcode 16.4 and builds the signed app with its bundled host. Real tabCapture runs on DrMeyers:
hosted macOS has an absent/intermittent Null Audio Device and is not a reliable rendered-tab
audio gate (https://github.com/actions/runner-images/issues/13668). The native workflow is
manual/nightly only. Its sink-only mode also runs tabCapture; full mode additionally attempts
the consent-dependent CoreAudio tap. Local validation uses macOS 27 / Xcode 27.
Failures preserve generated audio/metadata and logs as CI artifacts. No real conversations
or microphones are used in unattended tests.

GNOME has separate real user-gesture/Xvfb, PulseAudio/PipeWire, GTK and Talk harnesses. Their
results are not Mac hardware evidence. A fully merged browser
scenario driver, MLS time-code/Talk HPB fixtures, cross-browser minimum-version runs, and
nightly soaks remain explicit migration work, not implied by a green vector suite. The canonical upstream is codemyriad/cassini-browser-protocol; the Mac app owns its approved
manual/nightly DrMeyers runner. Neither runner setup nor a synthetic fixture grants TCC consent.

SPEC v1.1 adds checked-in macOS/GNOME native-session WAV fixtures, offline Git pin-verification
regressions, and controlled Teams mixed-receiver / Zoom PC-free / Meet switching-slot browser
models. Product account/version observations remain manual in products/results/.


The independent-session capability adds `tests/independent-sessions.mjs`: two
real Chromium WebRTC calls, separate native host connections and folders, both
tabs muted and an explicitly private Pulse sink muted. It checks two isolated
tones, media-clone deduplication, and one meeting continuing after the other's
Stop. It requires CASSINI_ISOLATED_AUDIO=1 and validates the private Pulse socket
and sink before mutating them; it refuses to run against a desktop sound server.
Use the GNOME private recording harness locally. Hosted CI starts its own server.
The default audio writer/control bridge are test fixtures, not product hosts.
CASSINI_NATIVE_HOST and CASSINI_CONTROL_NATIVE_HOST select actual app adapters;
CASSINI_TEST_EXTENSION and PLAYWRIGHT_MODULE select a pinned installed extension
and local Playwright. The generated launchers force all output into the private
fixture Library. Failure artifacts keep recent control events and manifests.

S-6 adds `conformance/session-events/`, copied unchanged from the coordination
fixtures with their origin recorded. `python3 conformance/check-session-events.py`
checks the schema, finite/ordered event times, the source-ID UTF-8 byte bound,
hand-written reader expectations and generated sparse WAV timelines. Unit tests
cover legacy/null annotations, unknown event types/fields, selection bounds and
the 1000-event cap. Neither schema validation nor these silent files prove a
writer's atomic event/track checkpoint: both apps must test crash/failure at
their real writer boundary and read these same fixtures independently.


D33 adds14 literal monotonic lifecycle vectors, checked by
`python3 conformance/check-call-lifecycle.py` and adopted unchanged by both apps.
The actual loaded-extension independent-session test also reloads a live call,
checks the original native connection/capture ID/folder and renewed PCM, closes
another tab, and verifies explicit terminal reasons through the fixture writer.
The fake-control driver does not prove desktop fallback or2s/120s real app timers;
those require the desktop's own adoption tests. Unit cases keep legacy loss behavior,
late old-document events, subframe bounds and invalid Stop side effects covered.

## D52 multi-stream wave 1

`python3 -m unittest discover -s tests -p "test_*.py"` checks the hand-written
lifecycle, outcome and half-open ownership vectors, actual generated public-speech
WAVs, per-source refusals, UTF-8 identity bounds and schema/pinning integrity.
The corpus test mutates presence/gaps and source ownership to prove the guards
fail; overlap bleed is checked against the independently pinned speech carrier.

`npm test` includes injected-clock retry/long-suspension cases and real Chromium
pre-join, source-switch, call-generation, worker-recovery and independent-session
cases. A receiver switch waits for the actual replacement waveform with unchanged
sample, power and bleed thresholds. Call-generation tests reuse a receiver,
exclude late old peers, and preserve intentional `callChanged` retirement.
Run only with a private audio server. Network-isolated containers need a private
bridge with a route for ICE, not a desktop audio/bus/home mount.
