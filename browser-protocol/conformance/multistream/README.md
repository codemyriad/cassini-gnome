# D52 wave 1 — draft conformance slice

- `outcomes.json`: hand-written per-source outcomes, including zero PCM,
  never-opened failures and sparse padding. `test_multistream.py` exercises the
  real framed browser fixture, per-source IO/limit/chunk refusals, duplicate
  declarations, overlap rejection, stale Stop and seventy source cycles.
- `lifecycle.json`: literal long-grace/rejoin/unknown-row/healthy-PCM/D49 cases.
  The Python policy is an independent oracle, not production code for either app.
- `ownership.json`: literal half-open D49 and lost/restored seams. Desktop
  adoption must exercise its actual mix/recognition/export path against them.
- `corpus.json`, `make.py`, `speech/`: first-cut production-shaped two-call corpus
  using pinned public LibriSpeech. Includes a late source, sparse presence,
  unequal source-key spans, D49/fallback/restoration, dropped gaps, -35 dB bleed
  and real overlapping speech. Run `python3 make.py DESTINATION` to materialize.
  Track `gaps` follow D52.2 (P7, see ../gaps/); `presenceWindows` only drive the
  generator. Sessions with a one-second gap are interrupted with problemKind gap.

The materialized-WAV regression checks exact half-open browser/system ownership,
actual lengths, zero-filled gaps, independent files, overlapping voices and the
-35 dB bleed against stored PCM. The initial overlapping fallback spans were
corrected; browser and system sources never own the same interval. Fixture-only
`speechStartMs` retains B's carrier phase through its resumed span, so the mic's
bleed follows the actual remote waveform at both single-voice and overlapping
turns. The latter check independently subtracts the pinned A carrier.

Desktop adoption runs the literal policy and seam cases through each app's own
policy, mixing and transcript adapters; the independent Python oracle is never
production code. Full native framing/host-edge and generated-peer tests run in CI.
`tests/call-generation.mjs` additionally proves a real in-page A/B/C switch with
A closing late and B's unchanged receiver reused for C. Intentional retirement
has no false source failure. Pre-join tests open no remote track until RTP flows.

Real-browser tests need a usable route for ICE even though all peers and HTTP
fixtures are local. A private Docker bridge with a route works; `--network=none`
or an internal bridge without a route may yield zero ICE candidates. Keep audio,
display, home and D-Bus isolated. No live product account, user media, desktop pin,
extension packaging or store upload is involved.
