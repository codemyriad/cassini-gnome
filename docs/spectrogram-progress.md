# D36 preparation (not a complete feature)

This work is parked on `wip/recording-spectrogram`. There is no D36 PR yet.
All visual surfaces, browser/broker wiring and production performance gates are
still pending. Main and the user's desktop are unchanged.

The streaming core uses actual libgstfft, S16 PCM, mono channel means, periodic
Hann, fractional band weights, fixed scale, exact frame-centre slots and gap
resets. Both native-size and 8192-frame chunking pass the frozen shared vector.
The test enforces the published tolerance (99% within one code, every cell within
three, exact DC and raw silence); this machine additionally matched every raw
and display cell exactly. The files and their normative reference hash are
pinned in tests/spectro-vectors/UPSTREAM.json.

The source owns bounded observers and per-side pending/output queues, chooses
at most three tracks per side, sums linear powers before quantisation, keeps
unselected histories warm and drops histories when the last observer leaves.
The native tap receives only committed selected PCM after native clock mapping,
after releasing the WAV/selection lock. It excludes readiness PCM, suppresses
retired callbacks and never turns a picture failure into a recording failure.
The tests compare saved PCM byte for byte, including an injected DSP failure.

Current validation: all seven Meson checks, 1,140 source tests and Ruff pass
(one unavailable-locale skip); 36 targeted DSP/native tests pass. All tests use
a private Ubuntu 26.04 container, without a desktop or audio-device connection.

## Next implementation work

- Keep lane collection/quantisation on the producer path; GTK draining now only
  dequeues completed columns. Native spans are declared before their first PCM,
  so simultaneous streams mix before a slot is sealed. Apply the same declaration
  ordering to the broker and restoration paths.
- Add the browser/native-fallback admission taps outside their writer locks,
  preserving restore transactions and excluding readiness/rejected PCM. Retire
  ended spans. Existing 100-ms AC levels remain the selection/silence authority.
- Add the private `meter` broker role, 40-ms bounded transport and per-window
  subscriptions. No extension/protocol change.
- Implement adaptive delay, ring texture release, pooling, continuous/sweep
  rendering, theme/warning/off/dropout states, a shared window frame callback,
  hidden/minimised shutdown and the exact visibility/lifetime tests.
- Add the second DC/speech golden using the committed normative reference;
  coordinate those bytes with Mac.
- Resolve the shared section 3.8 rounding ambiguity before shipping. This WIP
  uses the published rate table's exact `2048/48000` duration, which gives
  24 kHz → 1024 and 96 kHz → 4096. Literal `0.0427` gives different sizes.
- Run all five **real 60-second** A–E CPU/frame/memory/state cases, then the full
  hosted gate. The waveform files must remain unchanged.

The accelerated core microbenchmark is only preparation: 60 simulated seconds
in tight loops cost 0.583 CPU seconds for two native tracks (0.97%) and 1.281 for
four selected plus one warm track (2.13%). It quantises each track separately,
excludes real grouping/capture/GUI and is **not** acceptance evidence. Measure
the completed production path before deciding whether the small C accelerator
allowed by the spec is needed. No performance threshold is waived.
