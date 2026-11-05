# Shared Library vectors — S-3a / D6 / D7

`python3 make.py OUT` needs Python 3.11+ and its standard library only. It uses no
network, desktop, audio device, app modules or current clock. OUT must be new or
empty. It builds `OUT/<case>/root` and `activity.json`; it never reads or writes
`expected/`. Every case uses now=1793107080000, Europe/Rome (27 October 2026,
14:18). Activity maps canonical IDs to recording/saving; readers normalise case.

Per D7 the suite has 22 cases: main + one combined case per rule group R1–R9,
plus empty, first-record and received-only, plus the nine shared test 8 cases (T8,
below). The longer rare-edge checklist is
not a release gate. `cases.json` is an explicit filesystem recipe. Expected
JSON files were written from the rules, using literal values with serialization
helpers, before updating or running the GNOME scanner. No app output has been
used to generate or bless them. Mac independently reviews every expected file.

Each app scans the generated root with the case activity, then emits exactly the
expected shape. `badge` is text or null. For a live badge, elapsed milliseconds
are max(0, now-startMs) when start is known, otherwise zero. Group order and titles
follow R6. `names` includes all speaking names; the formatter later applies the
three-name limit to row meta. `speakers` contains only id/name/colourIndex; naming
provenance remains in the source metadata for later UI consumers. `scan` is empty
if a successful scan has no listed meetings, failed for an unavailable root.

Main follows sample-data.json and boards 01/05/07/17/29: nine deterministic IDs,
Budget review selected by the future shell, live Weekly sync at 12:48, Client
call in progress, Interview 14, interrupted Design crit at 00:41:12, received
Onboarding, failed Interview 15, Weekly sync on 20 October, Planning Q4. The extra
Interview 15 time/duration and older meeting durations are fixture choices where
the boards leave them open. Existing board timestamps, titles and text are kept.

WAVs use the wave module, PCM16, and actual complete frame counts. Rule cases are
one second long. Board-duration WAVs contain a one-second integer square tone
then sparse silence, so their *logical* frame counts give the real 52/47/63-minute
etc. durations while disk allocation stays small. Sparse holes read as PCM zero;
no metadata-only fake duration is used. Onboarding's 34-minute Opus reference has
102001 real silent Opus packets, correct pre-skip/final trim and a matching audio
digest. It is 421 kB, not a 60-ms asset with a fabricated duration.

Assets have SHA-256/size/provenance in assets/SHA256.json. Original conformance
references (including two transcripts and bad digest) are byte-identical CC0
files from cassini-format 867f68c56292ada9464b8516dc2797003d5af9eb. New CC0
references can be rebuilt with assets/build.py; make.py only copies the committed
bytes. No Cassini app encoder is involved. All S-3a files are CC0-1.0.

`python3 verify.py` checks structure, coverage, asset pins and two independently
built trees for identical bytes, modes, file types and link targets. It does not
validate expected row values by running an app. Main UI preference/search/voice
stores are outside S-3a; this supplies the Library and its activity source only.

## Shared test 8: three outcome files (T8, D67)

Four browser-capture cases (D67.5: source `chromium`, no native system audio in
v1), each one ten-second `Recording-` folder with session.json, two 16 kHz track
WAVs, meeting.json (with `recordingDetails.captureRecord`, the same shape
GNOME's `capture_report.from_session` writes) and transcript.json. The
microphone source and the guest "Ana" are both announced in `captureOutcome`.

| Case | Guest | Write-down | Distinct state |
|---|---|---|---|
| `t8-control-others-spoke` | 10 s, 5 s tone; captured | done; 3 + 2 words | control |
| `t8-others-silent` | 10 s of zeros; captured (zero-valued audio is captured: protocol `outcomes.json`) | done; 3 words; `silentTracks: ["guest"]` | Details adds "Ana: no speech heard." (D67.1) |
| `t8-others-never-captured` | 0-frame WAV; neverAudio; session interrupted (`problemKind: capture`) | done; 3 words; `silentTracks: ["guest"]` (D67.3) | "Ana: no audio was received." wins over silent (D67.1); Interrupted at 00:00:10 |
| `t8-others-transcription-failed` | 10 s, 5 s tone; captured | failed as the writer leaves it (D67.2): `reference: null`, stored error, 2 partial words kept in an unreferenced transcript.json for Try again | Needs attention; no words |

Five guard cases pin what those four cannot tell apart. Their lines are not
required to differ from the four states.

| Case | Pins | Files | Shown |
|---|---|---|---|
| `t8-failed-referenced-words` (also R3) | D67.2 "readers show the file as stored": no reader rule hides a failed meeting's words | failed, but meeting.json still references a readable sidecar with 3 words (an older or another writer) | Needs attention; names Me, Ana (R7); no quote (R8.5); the 3 words are visible |
| `t8-retry-failed-stale-silent` | R13.1 "the transcript is done" | both tracks zeros; the first write-down was done with no speech (`silentTracks` lists both); Try again failed, so that writingDown is still stored | Needs attention; no silent line |
| `t8-silent-per-source` | R13.1 "every track", R13.3 place | rejoins (D63 #1): Bruno's two tracks both silent, Ana's first track silent and her return track spoke; sources mic, Bruno, Ana | "Bruno: no speech heard." right after Bruno's line, before Ana's; none for Ana |
| `t8-none-referenced-words` (also R3) | D67.8a "shows them as stored, as for failed" | status none, never written down here, but meeting.json references a readable sidecar with 3 words (another or an older writer); same audio and words as `t8-failed-referenced-words` | no badge; names Me, Ana (R7); no quote (R8.5); can write down (R9); the 3 words are visible |
| `t8-low-audio-silent` | D67.8b, R13.1 "captured" | Ana sent 500 ms of zeros (lowAudio; session interrupted, `problemKind: capture`); done; `silentTracks: ["guest"]`, her only track | Ana's `details.sources.lowAudio.underSecond` warning ("less than a second", D58.4) and no silent line; Interrupted at 00:00:10 |

`expected/t8-*.json` are rows as for the other cases. `outcome-lines.json` adds,
per case, the Details "Audio sources" lines as catalog ids with their placeholder
arguments (LIBRARY-RULES R13), whether the meeting reads as no speech, and the
visible words per speaker id. Both files are hand-written from the rules, never
from app output. Each app renders the lines through its real Details path and
compares ids, arguments (rendered with its own catalog and meetingDuration) and
the warning role exactly.

Tones longer than one second use the optional wav key `toneFrames` (here 80000,
five seconds, well above the D56 silent floor). Without it the tone is still
`min(frames, rate)`, so the 13 earlier cases are byte-identical; `verify.py`
re-checks their pinned tree digests, and those of the seven T8 cases
before D67.8. It also checks that `outcome-lines.json`
covers exactly the T8 cases, its catalog ids and placeholders against
`../strings.json` where that file is beside the vectors (ux-draft), that the
four outcome cases' lines differ pairwise, with the silent case's Details lines
differing from the control's, and the shape each guard case relies on.
