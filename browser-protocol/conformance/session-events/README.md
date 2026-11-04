# S-6 source events fixture

CC0-1.0 synthetic silent PCM and metadata. `session.json` is version 1, with
the optional S-6 events. `expected.json` is written by hand from SESSION-EVENTS.md.

Run `python3 make.py /temporary/Library` to create a real folder with sparse
PCM16 WAVs. No speech or personal recording is used. Silence is deliberate;
this fixture checks timeline metadata and reading, not recognition or meters.

The browser path is lost at 754210 ms; a new system track starts there while
the microphone continues. At 900000 ms the person chooses Me, finalising the
system track and recording both the selection and the `none/user` source event.
The microphone continues to 960000 ms. The final selection is Me only, so the
fallback line is absent then. No return to the browser is inferred.

Readers ignore unknown event types and fields. A v1 folder without events has
no inferred fallback line. Export mixes the three WAVs by their explicit offsets;
events themselves are not exported in wave 6.

`python3 make.py /temporary/Library --case browser-off-on` creates the second
case. The browser side is off from 5000 to 10000 ms. Its resumed span has a new
ID and WAV but the same opaque `sourceTrackId`, so both spans write down as one
speaker. The browser label deliberately changes: labels never establish identity.
The microphone omits `sourceTrackId` and retains the source speaker Me.

`restored.reader.json` preserves the historical reader-acceptance vector. D33 now
also allows that writer transition. `--case call-return` materialises the new
browser → computer → browser case in one folder. Its returned span shares the
actual opaque remote source ID, and the final single `end` explains callEnded.
The hand-written expected file checks timeline, roles and final end. Schema and
semantic checks reject illegal restoration, duplicate/nonterminal end and unsaved
state with end. Policy deadlines live in `control/call-lifecycle-vectors.json`.

`--case uncommitted-return` is the same call returning before its fallback
committed: the browser loss writes none/lost at 1000 ms, no system track starts
(the fallback was not asked for yet, or was still waiting, for instance on the OS
permission prompt), and
browser/restored with a new span of the same opaque source follows at 3500 ms
(SPEC-CONTROL "Return before the fallback commits"). Restored is legal after
none/lost as after system/missing or system/lost; none/user is not a fallback.
`--case uncommitted-return-kept` is the same return from a writer that keeps its
written othersSource while a fallback is pending (SPEC.md: "until then the
recording keeps its written othersSource"): no none/lost and no restored. Both
expected files require the same Others presence, `othersPresenceMs` (span extents
from each track's D55 placement, minus gap ranges, D52.2): absent from the loss to the return, so neither form may
hide the missing audio, as a zero-filled span would.

Production-shaped speech lives elsewhere. `../multistream/` holds the D52 corpus,
`corpus.json` plus `make.py`. It has real pinned speech, a late remote, sparse and
gapped spans, a real D49 switch and lost/restored seams with physical overlap, bleed in
both directions, overlapping voices and two concurrent folders. That corpus has named
`constants` and hand-written `expected` literals. These S-6 cases stay metadata-only.
