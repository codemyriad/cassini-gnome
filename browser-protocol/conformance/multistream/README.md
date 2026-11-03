# D52 wave 1 — draft conformance slice

- `outcomes.json`: hand-written per-source outcomes, including zero PCM,
  never-opened failures and sparse padding. `test_multistream.py` exercises the
  real framed browser fixture, per-source IO/limit/chunk refusals, duplicate
  declarations, overlap rejection, stale Stop and seventy source cycles.
- `lifecycle.json`: literal long-grace/rejoin/unknown-row/healthy-PCM/D49 cases,
  plus D52.4 control-loss and `transportAlive` (audio transport closed) cases,
  and D52.5 ask-once resume cases: case fields `resumeCapture`/`callGeneration`,
  row fields `callGeneration`, `controlConnection` (changes only with a new
  hello) and `resumeRefused`, and the action `askResume` (see the vector note).
- `stale-watchdog.json`: literal `PCM_STALE_AFTER` (45 s, `>=`) cases with the
  independent oracle `stale_watchdog.py`: audio-transport frames in, close
  time out; track/sources keepalives, armed vs unarmed spans, the exact
  boundary and control frames that do not count; D57 document retirement, and
  since D61 tracks that carry a documentGeneration (dormant exactly when that
  generation departed; an invalid one counts as absent). Frames are in the app's own
  receipt order across the control connection and the audio transport.
  The Python policy is an independent oracle, not production code for either app.
- `ownership.json`: literal half-open D49 and lost/restored seams. Desktop
  adoption must exercise its actual mix/recognition/export path against them.
- `corpus.json`, `make.py`, `floor.py`, `speech/`: the production-shaped two-call
  corpus v2, using pinned public LibriSpeech speech. See "Corpus v2" below.

## Corpus v2

`python3 make.py DESTINATION` materializes two Library folders in about 2 s. It is
stdlib only, so `/usr/bin/python3` on macOS works. The output is byte-identical on
Linux and macOS, and `materialized.sha256` pins it. Apps read `constants` and `expected`
from `corpus.json`. They never hard-code the numbers.

**Constants.** Every threshold lives in `constants`:

| Name | Value | Meaning |
|---|---|---|
| `LATE_REMOTE_MS` | 30000 | some source's first span starts at or after this |
| `SPARSE_DENSITY_MAX` | 0.5 | presence/span and speech/span of the sparse span |
| `SPAN_RATIO_MIN` | 5 | max/min span length within one sourceTrackId |
| `BLEED_DB` | -35 | both bleed directions |
| `D49_OVERLAP_MS` | 2000 | physical system/browser overlap at callConnected |
| `SEAM_OVERLAP_MS` | 1000 | physical overlap at the lost and restored seams |
| `OVERLAP_MIN_MS` | 2000 | double-talk with me, and two remotes at once |
| `VOICES_MIN` | 3 | distinct pinned voices |
| `CONCURRENT_MIN_MS` | 20000 | wall-clock overlap of the two folders |
| `NOISE_BED_DBFS` | -45 | the noise bed on b-return |
| `FLOOR_TOLERANCE_DB` | 3 | app floors against the oracle bands (16 kHz/512 vs 48 kHz/1536) |

**Voices.** A is speaker 1089 (me), B is speaker 260 (source-b, and the system
path, which carries the same call) and C is speaker 1221 (source-a). The two remotes that
overlap are different people.

**Session 1** (`…0001`, 90 s). Its events are system/missing@0, browser/callConnected@20000,
system/lost@50000, browser/restored@60000 and end/user@90000.

| Track | Span (ms) | Speech | Other content |
|---|---|---|---|
| mic | 0–90000 | A 0–8 s, 10–18 s, 32–38 s, 60–68 s | B bleeds in from b over 20–28 s and from b-return over 60–68 s, at BLEED_DB |
| system | 0–22000 | B 18–22 s, in phase with b | owned only to 20000 |
| a-short | 30000–32000 | C | the late remote; `droppedFrames` 4800, a drop after its last frame |
| a-long | 40000–50000 | C 40–41 s, 48–50 s | jump gap 41000+7000 ms; `droppedFrames` 336000 |
| a-return | 60000–62000 | C | |
| b | 18000–50000 | B 18–28 s, 49–50 s | A (my echo) bleeds in over 32–38 s at BLEED_DB; owned from 20000 |
| system-fallback | 49000–61000 | B 49–58 s, 60–61 s | input gap 54000+1500 ms; owned 50000–60000 |
| b-return | 60000–90000 | B 60–68 s, 72–84 s | jump gap 70000+2000 ms; noise bed inside presence |

- **The D49 switch.** Over 18–22 s, system and b hold identical bytes: one call captured twice.
- **The seams.** b and system-fallback are identical over 49–50 s. system-fallback and
  b-return are identical over 60–61 s, including the noise.
- **b-return** is the noisy, gapped, fallback-sized remote. b stays clean. That gives source-b
  two spans whose floors are far apart, which is the shape per-person pooling needs.
- **Capture facts.** Per D52.2, every track records `gaps`. Browser tracks also carry
  `receivedFrames` (frames minus gapFrames) and `captureOutcome`, and `captureOutcome.sources`
  lists source-a and source-b, both captured. State and problemKind come from the gaps
  alone (interrupted, gap). `droppedFrames` never changes them.

**Session 2** (`…0002`, "Concurrent call B") starts at 12:00:20 and runs 40 s. Its one
remote track carries A. It overlaps session 1 in wall time from 12:00:20 to 12:01:00.

**Generator format.**
- `speechWindows` entries are `[a, b]` or `[a, b, phaseMs]`. Voice position 0 plays at
  `phaseMs`, which defaults to the track start.
- `bleeds` lists `{from, db, windows}`. A bleed copies that track's own speech and noise,
  never its bleeds.
- `noise` is `{dbfs, seed, originMs, windows}`. It is uniform xorshift32 noise at 16 kHz,
  seeded from sha256(seed) and held for three frames like the speech. Tracks that name the
  same bed carry the same samples.
- `presenceWindows` only mask the generator. The 16 kHz carriers are held for three
  frames and halved, so they are fixture carriers, not a recognition resampler.

**`expected`** holds hand-written literals. make.py never writes them, and
`tests/test_multistream_corpus.py` checks them against the independent oracles.
- **Speaker keys.** `me` is the microphone, `others` is the system path, and every other
  key is the sourceTrackId.
- **Ranges** are half-open session milliseconds, except `presence16k`, which is in 16 kHz
  session samples.
- **Per track:**
  - `ownership` comes from `ownership.windows`.
  - `presence16k` is `gaps.presence` with that ownership.
  - `gaps` are the stored gap ranges.
  - `noWords` is the span minus owned presence: gaps plus non-owned overlap. No word may
    land there.
  - `floorDb` is the band for the span floor.
- **Per session:**
  - `speechMs` and `overlaps` (with the people in each) are computed over owned presence.
  - `personFloorDb` gives the per-person bands.
  - The rest are `voices`, `d49Overlap`, `seamOverlaps`, `bleeds`, `noiseBed`,
    `captureOutcome` and `droppedFrames`.
- **Floor bands.** Each is the oracle floor rounded to a whole dB, ± FLOOR_TOLERANCE_DB.
  An app's own floor for the span or person must fall inside it.

`floor.py` is the independent floor oracle (G9). It is never production code.
- A span floor is the 10th percentile of audible 32 ms frames inside presence (with
  ownership), measured on the stored 48 kHz PCM.
- A person floor is the minimum over that person's spans.

**Tests.**
- `shape()` asserts one requirement per message.
  - **R4 D49 and seams.** A system/browser pair only counts when both tracks carry the
    same call over the overlap: the same voice and phase, noise bed and bleeds. The D49
    overlap must carry that call on both sides of callConnected.
  - **R6 bleeds.** Every bleed has windows where its source speaks and the target is
    present. Each direction needs a BLEED_DB bleed that is audible somewhere the target
    itself is quiet.
  - **R6 voices.** Each person speaks with one pinned voice.
- The self-test applies at least one data mutation per requirement. Each must fail with
  that requirement's message. These include the audit probes (a-short moved to 0, the
  mic's 60–68 s speech removed, a-long shortened by 1 ms) and the review probes: a wrong
  seam partner, out-of-phase or reseeded overlaps, a system track that stops at
  callConnected, empty or silent bleed windows, an echo masked by the target's own
  speech, and two voices for one person.
- `tests/test_corpus_oracles.py` patches `gaps.presence` (ignoring gap ranges, or
  ignoring ownership), `floor.person` (pooled by max or by mean) and `floor.span`
  (ignoring presence, or ignoring ownership). Under each mutant, the same `expected`
  check must fail. Stored gaps are zero-filled and the corpus has no DC offset, so the
  span mutant that ignores gap ranges only and a level without mean removal must fail
  synthetic oracle checks instead.
- The materialized checks cover:
  - stored lengths and zero-filled gaps
  - every expected bleed at BLEED_DB ±0.1 dB, the target minus its own pinned carrier
    (so b-return into the mic is checked under my speech)
  - the three-voice residue at 60–62 s after subtracting the pinned A and C carriers
  - byte-identical D49 and seam overlaps, for the pairs `shape()` finds
  - the noise level
  - the oracle floors inside their bands
  - the pinned digests. Without the repo's Python test deps, `/usr/bin/python3 make.py
    DEST && (cd DEST && shasum -a 256 -c PATH/TO/materialized.sha256)` checks the same pin.

## Adoption and browser tests

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
