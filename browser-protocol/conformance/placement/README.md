# Placement golden (shared test 9, D55)

One set of tone-burst folders; GNOME and Mac must both land every burst where
D55 says, in every consumer: within 20 ms everywhere, and exactly (to the
sample) wherever no resampler is involved. Each app is checked against the
oracle here, never against the other app.

```
python3 conformance/placement/make.py OUT        # three Recording-<id>/ folders
python3 conformance/placement/placement.py check RESULTS.json
python3 conformance/placement/make.py --mutant NAME OUT   # the app mutants, below
python3 conformance/placement/placement.py check --mutant NAME RESULTS.json
python3 conformance/placement/placement.py compare GNOME.json MAC.json
```

## Files

- `bursts.json`: the sessions (inputs to `make.py`), the app `mutants` and, under
  `expected`, every burst's position per consumer. `expected` must equal
  `placement.py expected`.
- `placement.json`: hand-computed D55 literals for the placement function itself.
- `placement.py`: the oracle, the shared onset detector, `check` and `compare`.
- `make.py`: stdlib only, integer samples; `materialized.sha256` pins its bytes
  on Linux and macOS.
- `tests/test_placement.py`: literals, mutants, digests, WAV read-back, checker.

## The folders

Every burst is 250 ms of a 1 kHz square wave of +-8000 starting with +8000;
everything else is exactly 0, so a burst's onset is its first non-zero sample.
Each track is 12 s with six bursts 2000 ms apart (not D56-silent). Bursts of
one session never overlap in session time, so a mix can be measured too.

| session | source | track | rate | channels | startOffsetMs | bursts at (local ms) |
|---|---|---|---|---|---|---|
| A `…0001` | gnome | microphone | 48000 | 1 | 0 | 500, 2500, … 10500 |
| | | system | 48000 | 2 | 1234.5104 | 0, 2000, … 10000 |
| B `…0002` | macos | microphone | 16000 | 1 | 1001.28125 | 0, 2000, … 10000 |
| | | system | 48000 | 2 | 31999.96875 | 1000, 3000, … 11000 |
| C `…0003` | gnome | microphone | 48000 | 1 | 0 | 500, 2500, … 10500 |
| | | system | 48000 | 1 | null | 1500, 3500, … 11500 |

The offsets separate every rounding seen so far, in the folders and not only in
`placement.json`: 59256.4992 frames at 48 kHz (ns-then-ceil gives one more);
16020.5 at 16 kHz, an exact half frame with an even floor (floor, half-even,
whole-microsecond rounding and ms / 1000 * rate in doubles all give one less);
and 1535998.5 at 48 kHz (floor and half-even give one less). B is a Mac-written
native session read by GNOME, A and C the other way round. C's system offset is
unknown: transcription and export place it at an assumed 0 and set
`offsetsAssumedZero` (D55 M9); playback refuses the whole session, its
known-offset microphone included (D19), as both apps' folder playback does.
Any new offset must be exactly on a half frame or at least 1e-6 frame from it at
16 and 48 kHz, so binary doubles round it the same way everywhere.

## What each app measures

D55: frame = offsetMs * rate / 1000, rounded half away from zero, at each
consumer's rate. A burst at local ms `t` of a track at `offset` is expected at:

| leg | what the app measures | expected | exact tier |
|---|---|---|---|
| `placement` | its one placement function on every `placement.json` case | the literal | equal |
| `rec16` | the 16 kHz recognizer input, as placed on the session timeline | placement(offset, 16000) + 16 t | 0 samples (the shared FIR keeps a 48 kHz burst's onset exact) |
| `wordMs` | its production word-time function, for a recognizer segment that starts at the burst's local 16 kHz sample 16 t with a word at 0 s in it | floor(rec16 * 1000 / 16000) | equal |
| `mix48` | the 48 kHz export mix before encoding | placement(offset, 48000) + 48 t | 0 (48 kHz track); 20 ms (16 kHz track, resampled) |
| `playback` | folder playback, as frames at the track's rate | placement(offset, rate) + t * rate / 1000, or `refused` for every track of a session with an unknown offset | 0 (48 kHz); 20 ms (16 kHz) |
| `offsetsAssumedZero` | per session, read back from what transcription (`writingDown.offsetsAssumedZero`) and export (`recovery.offsetsAssumedZero`) wrote | true for C, false for A and B | equal |
| `opus48` (optional) | the published Opus mix, decoded at 48 kHz | as `mix48` | 20 ms |
| `mix48publication` (optional) | a second 48 kHz mix path, if the app has one (GNOME's publication mixdown) | as `mix48` | as `mix48` |

`wordMs` pins its inputs because the production functions add the segment start
and the word's start in it separately: with any other segment start a correct
app can be 1 ms off. Never feed it the measured `rec16` onset.

Every leg must also be within 20 ms (the headline). Find each burst with the
shared detector (`placement.onset`): in [e - 0.05 R, e + 0.2 R) of the
expected sample e at the stream's rate R, average stereo to mono, and take the
first sample whose magnitude reaches half the window's peak. On a stream that
was not resampled this is the first non-zero sample. A burst not found is
`null`, which fails. Positions are whole numbers: NaN, fractions and booleans
fail, and `check` refuses NaN, Infinity and repeated keys in the file.

## Results file

Each app writes one file and runs `placement.py check` on it (or the same
comparison in its own test). Keys are `<session id>/<track id>`, except
`offsetsAssumedZero`, keyed by session id; lists hold one value per burst, in
order. An excerpt (`…` marks what is left out):

```json
{
  "vector": "placement-v1",
  "protocolCommit": "<the vendored commit>",
  "app": "gnome",
  "legs": {
    "placement": [[0, 16000, 0], [0, 48000, 0], [1234.5104, 16000, 19752], …],
    "rec16": {"09000000-0000-4000-8000-000000000001/system": [19752, 51752, 83752, 115752, 147752, 179752], …},
    "playback": {"09000000-0000-4000-8000-000000000003/system": "refused", …},
    "offsetsAssumedZero": {"09000000-0000-4000-8000-000000000003": {"transcript": true, "export": true}, …},
    …
  }
}
```

`placement` lists every `placement.json` case in order as `[ms, rate, frame]`.
`rec16`, `wordMs`, `mix48`, `playback` and `offsetsAssumedZero` are required and
cover every track (session); `opus48` and `mix48publication` are optional. An
unknown leg or track fails.

## App mutants

`check` only compares numbers, so an app could pass by reporting `expected`.
Each app therefore also measures its legs on the three `mutants` in
`bursts.json` (`make.py --mutant NAME`: one track offset moved in one
session.json) and runs `check --mutant NAME` on those results. A mutant passes
when the named legs fail in its session (optional legs only if reported) and
nothing else fails:

| mutant | change | must fail |
|---|---|---|
| `a-system-late` | A.system 1234.5104 → 1234.5312: one 48 kHz frame later, the same 16 kHz frame | `mix48`, `mix48publication`, `playback` (exact tier) |
| `b-mic-25ms` | B.microphone 25 ms later | every position leg (headline) |
| `c-system-known` | C.system null → 0: the same placement, but known | `playback`, `offsetsAssumedZero` |
`compare` prints each burst where two apps' files differ, for coordinator QA.

## Not covered (v1)

The published mix of a meeting with an unknown offset (GNOME plays it, Mac
refuses it; R87 Q6); recognition through the real VAD (tones are not speech, so
word times are checked through the word-time function, not through Silero); the
word end clamped to the segment's PCM; rotated and clock-anchored spans; and a
0-frame track with an unknown offset (ignored by playback and by the export
flag in both apps).
