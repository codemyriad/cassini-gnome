# Validation record — 2026-10-27

Validated the recording-only staged source snapshot independently of unrelated
packaging/runtime edits in the development checkout.

Environment: Linux, Python 3.14.4, GTK 4.22.4, libadwaita 1.9.1,
GStreamer 1.28.2, Playwright 1.61.1, Chromium 149.0.7827.55.
Real meeting integration: Nextcloud 35.0.0 with Talk 25.0.6, using the adjacent
Go Cassini harness's core services in a dedicated Compose project.

Passed:

- 75 pytest tests in the recording-only snapshot; all GStreamer tests executed.
- Three AudioWorklet tests.
- Ruff lint/format, Meson build/install, desktop/schema/AppStream validation.
- Installed native-host registration and framed error response.
- Native PulseAudio capture from private 440 Hz microphone and 880 Hz desktop
  sources, Opus finalization/decoding, channel frequency and leakage checks.
- Real Chromium extension activation and Native Messaging, with separate real
  peer connections: local 440 Hz / remote 880 Hz separation, mute silence,
  660 Hz sender replacement, shared-timeline offset, removed-preview exclusion,
  repeated recording, navigation interruption, 770 Hz media-stream fallback,
  990 Hz mixed-tab fallback, and render-timeline agreement with elapsed time.
- Real GTK controls, recording finalization on quit, session opening and
  source playback/pause. The test compiles its own private GSettings schema.
- Real Talk two-guest call with generated browser devices: separate audible
  local/remote WAVs, valid headers, clean completed-session manifest.

The tests found and fixed a GTK translation callback shadowing bug and initial
AudioContext timestamp calibration, exercised
Chrome's actual extension-invocation grant, and prevented implicit Wayland
selection by forcing X11. All GUI/audio integration runs now use the private
Xvfb/D-Bus/XDG/PulseAudio environment documented in README.md.

The media is synthetic. These results do not establish microphone hardware,
Bluetooth hotplug, every meeting service, participant-name mapping, or
sandboxed browser/Flatpak Native Messaging compatibility. Those remain separate
compatibility work, not prerequisites silently skipped by these tests.
