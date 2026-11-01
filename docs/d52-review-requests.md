# D52 wave 1 — pending shared review

These are review proposals, not published contract or catalog changes. DrMeyers
SSH is unavailable; the matching REPLIES/STATUS append is saved on roy. #143
and #144 stay draft until the shared decisions and final integration are settled.

## R63 — source-report copy

| ID | English | Role / note |
|---|---|---|
| `details.sources.title` | Audio sources | section title; Recording details: source accounting, including a source that never opened a WAV. |
| `details.sources.captured` | {source}: {duration} recorded. | status line; Received audio includes zero/DC samples; padding is excluded. |
| `details.sources.neverAudio` | {source}: no audio was received. | warning line; Finished capture only. The source was announced but delivered no admitted PCM. |
| `details.sources.lowAudio` | {source}: only {duration} of audio was received. | warning line; Finished capture only. More than zero and less than the shared 1000 ms threshold. |
| `details.sources.failed` | {source}: audio could not be recorded. | warning line; Sticky source-local failure, including before any WAV was opened; other sources may be healthy. |
| `details.sources.gap` | {source}: {duration} of audio is missing. | warning line; Duration from explicit dropped/padded frame counters, never from quiet audio. |
| `details.sources.empty` | No audio sources were reported. | status line; Unknown inventory, never a claim that everybody was recorded. |
| `recording.sourceProblem` | Some meeting audio could not be recorded. | warning line; Live Recording page only after a source-local terminal or IO failure, not normal startup. |
| `details.reason.callConnected` | The call connected, so Cassini began recording voices from the browser. | explanation; D49 system/missing → browser/callConnected, distinct from restoring a temporarily lost source. |
| `details.sources.overlap` | Overlapping audio spans were kept as separate speakers. | diagnostic line; G1 refuses to sum conflicting spans of one opaque source identity. |

The coordinator publishes the shared languages and exact catalog bytes. No
provisional copy is rendered by these PRs. Live startup neverAudio/lowAudio facts
are not warnings; actual source failures are. Final source facts retain the
reported outcome even for zero PCM. Padding and reported drops are separate
counters: without ranges, adding them can double-count the same hole.

## R64 — overlap attribution

A concrete candidate and literal expected IDs are in
[d52-source-grouping.md](d52-source-grouping.md). The proposed suffix is now
`base:overlap:n`, replacing `~overlap-n` because D24/D25 reserve tilde prefixes
for child voices. Known extents use validated metadata and scalar clock mapping;
unknown timing never proves disjointness. No NumPy or audio I/O is needed.

## R65 — portable source record

#144 proposes optional `recordingDetails.captureRecord`, version 1, carried
through the existing `x["codemyriad.io"].meeting` mapping. It contains source
outcome rows, whitelisted track diagnostics and S-6 events, preserving future
fields. Its real publication/cache-deletion tests retain all 70 announced
sources even with one WAV. Shared schema/mapping approval remains pending.

## R67 — the initial system stand-in needs a resumable Start intent

A broker-only fresh-token alias is unsafe: an event-synchronized actual RPC
test stops stand-in F, then delivers pending native Start X. Stop F cannot
cancel unknown X, and a second recording is created. A call generation identifies
the call, not the Start operation; blocking that generation would also reject
a legitimate later manual Record. The prototype remains isolated on
`wip/stand-in-transport-adoption`, not in these PRs.

Proposed shared opt-in `resume-capture-v1`: Start may target the active stand-in
`{captureId: F, timelineOriginMs: original}` with the selected callGeneration.
The extension uses F/original origin from session construction, including early
status, native Start and scoped Stop. The native ACK confirms resumption, and a
stopped or changed target is refused. Fresh manual Record remains unchanged.
Mac should use the same intent and vectors. A local extension update needs a
version bump. No protocol change has been made for this proposal.

Shared tests should cover both status/native delivery orders, Stop before Start
or session construction, a fresh manual Record after Stop, stale A/B signals,
retained microphone/selection and a half-open D49 seam. Full reproduction:
[stand-in spike notes](https://github.com/codemyriad/cassini-gnome/blob/wip/stand-in-transport-adoption/docs/d52-stand-in-spike.md).

## R69 — deliberately unselected sources must not flag a capture failure

A newly discovered remote while The others is off currently becomes neverAudio
and marks the otherwise healthy microphone meeting interrupted. The real writer
receives that PCM but intentionally discards it. This is a G2 accounting bug.
Proposed optional source-row `captureExpected: false` means that source was
never selected for admission. Missing means true for older records. Outcomes
and admitted frame counts stay factual; only expected missing/short/failed
sources flag the recording. The flag turns true permanently if that source is
ever selected or a recording span opens; switching off cannot forgive an
earlier capture failure. A failed unused source must not stop healthy audio.

The existing session v1 schema permits this additive property; no protocol
snapshot/schema is edited. R65's proposed portable record carries the same bit,
and the immutable source model suppresses failure styling for an unused source.
This still needs shared agreement before landing. Proposed R63 additional copy:
`details.sources.notSelected` — “{source}: not selected for recording.”
Role: neutral source status; note: final/live source was never selected, so its
absence is intentional. No speculative selection timing is inferred by a reader.
