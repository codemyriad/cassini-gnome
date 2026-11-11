# D88 main audit, 2026-11-11

Baseline: `870b2e63739c404025fa1c417f0f2b5bc89781f3`.
Scanned all 578 tracked test-source files and build-aux/CI shell, Python and
workflow files, including the vendored browser tests. The conservative timing
scanner reports 2,161 discovery sites: 85 delay candidates, 389 clock reads,
923 temporal assertions, 569 bounded waits, 107 timer callbacks and 88 clock
conditions. These are candidates, not a count of D88 violations: sample clocks,
injected clocks, CPU measurements, timer stimuli and hang guards also match.
The complete path/line/expression evidence is retained at
`/mnt/ai/cassini/dev/evidence/gnome-d88-main-audit-20261111/audit.csv`.

## This area: Library and Preferences observations

`tests/ui/{library,search,settings,speakers,rename,import-export}.py` dispatch
one actual GLib event at a time and inspect the existing completion predicate.
Their 5/10 ms polling sleeps and manual monotonic-deadline loops are removed.
The shared dispatcher uses a cancellable timeout source solely as a hang guard.
Rename retains its native-key and dialog diagnostics. Import/export's existing
single-dispatch chooser observer remains in place. All product assertions,
real X11 key events, playback sample positions and screenshot checks remain.
No transcript production code changes.

## Remaining work, grouped for later area PRs

- Model Boards: voice-models, speech-models, diarization-models and model-notices
  are already covered by #366, which is under review at this baseline.
- Other GTK observers: background, companion, companion-sandbox, interjections,
  journey-parity, meeting, recording-details, voice-preparation, voices and
  what-is-recorded; also the shared `painted_node` and recording readiness
  helpers. Match the producer's notification before replacing each poll.
- Integration observers: Desktop/windowless, browser-setup/installed,
  recording/{app-control,browser,call-signals,companion-address,companion-install,
  flatpak-companion}, and diarization/voice/Whistle queue probes still contain
  polling readiness loops. Keep sample-count evidence and actual elapsed-media
  stimuli distinct from synchronization.
- Unit helpers: browser-service, library-monitor, recorder and transcription
  still have polling helpers. Long sleeps in synthetic stuck subprocesses
  (meeting-job-cancel, transcription, whistle-spike) are cancellation stimuli;
  replace them with explicit gates where practical, preserving the blocked
  child the test must kill. Speech-model cancellation cases are in #366.
- AT-SPI step budget: the progress guard currently wakes periodically; use
  actual step progress plus a one-shot hang guard in a later UI area.
- CI process cleanup: `build-aux/bounded-run.sh` polls the owned systemd scope
  and marked descendants after killing them. These are bounded cleanup waits,
  not success delays; a future harness area can use process-exit observation.
  No workflow YAML fixed sleep was found.
- Vendored browser-extension and browser-protocol tests include worker-recovery,
  generation, reload, socket and lifecycle polling. Propose shared changes to
  their owner; do not edit the vendored copies.

Transfer retry backoff (`tests/parakeet/prepare_model.py`) is operational retry
policy. Browser capture/render stall intervals deliberately exercise stalled
producers. Neither is a readiness sleep to delete blindly. Fake-clock and
thread/process CPU assertions remain legitimate under D88.
