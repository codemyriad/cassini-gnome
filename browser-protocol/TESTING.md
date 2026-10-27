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

Mac CI runs layers 1–4 on macos-15 / Xcode 16.4 and builds the signed app with its bundled host.
Local validation used macOS 27 / Xcode 27; the newly configured CI has not yet been executed.
Failures preserve generated audio/metadata and logs as CI artifacts. No real conversations
or microphones are used in unattended tests.

GNOME has separate real user-gesture/Xvfb, PulseAudio/PipeWire, GTK and Talk harnesses. Their
results are not Mac hardware evidence. Shared publication upstream, a fully merged browser
scenario driver, MLS time-code/Talk HPB fixtures, cross-browser minimum-version runs, and
nightly soaks remain explicit migration work, not implied by a green vector suite. No new
remote repository or self-hosted CI runner has been provisioned.
