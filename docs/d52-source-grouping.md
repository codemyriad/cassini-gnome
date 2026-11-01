# D52 conflicting source spans — R64 candidate

This draft proposes concrete write-time speaker IDs for shared review. It does
not change a shared schema or vector. The PR remains draft until the coordinator
and Mac settle these IDs; normal disjoint sources keep their existing IDs.

The preparer groups a browser source's spans only when their exact half-open
physical extents are disjoint. Worker grouping applies the same guard if it
receives raw track descriptions. Clock-mapped ends retain their 48 kHz sample
precision rather than flooring them to milliseconds. Unknown offsets never use
display's assumed zero to prove disjointness; each unknown span stays separate.
PCM, source keys, labels and timing are unchanged.

For each base speaker, known spans sort by exact start, end and opaque track ID;
unknown spans sort last by ID. Each takes the lowest lane whose last end is at
or before its start. An unknown span always takes a new, non-reusable lane.
Lane zero retains the base ID. Further lanes use `base:overlap:n`, starting at 1.
Every ordinary base ID is reserved first, and suffixes skip an occupied ID.
Input order does not affect the assignments. Reapplying the operation is a no-op.
The tie-break is the validated capture track ID, never the storage basename;
compaction may rename the WAV/Opus file while that identity stays unchanged.
The recognition job carries it as `captureTrackId`, separately from its local
progress/file key. This is an internal job field, not a shared format change.
Opaque IDs compare by UTF-8 bytes without Unicode normalization, including IDs
whose human-readable spelling is canonically equivalent.

The colon suffix replaces the earlier proposed `~overlap-n`: D24/D25 use `~`
to identify diarized children. A conflicting source must not be mistaken for a
child voice and excluded during merged-voice extraction. Opaque source IDs can
contain the candidate suffix, hence the reservation rule.

Literal expected review cases (`tests/test_source_spans.py`):

| Base | Spans in ms | Stored speaker IDs |
|---|---|---|
| `track-receiver` | a [0,1000), b [1000,2000), c [2000,3000) | a, b, c: base |
| `track-receiver` | a [0,2000), b [1000,3000), c [2000,3000) | a/c: base; b: `track-receiver:overlap:1` |
| `track-receiver` | a [0,1000+1/48), b [1000,2000) | a: base; b: `track-receiver:overlap:1` |
| `track-receiver` | a [0,1000), b/c unknown | a: base; b/c: `track-receiver:overlap:1` / `:2` |
| `track-receiver` | a/b [0,1000); another base is `track-receiver:overlap:1` | a: base; b: `track-receiver:overlap:2`; other unchanged |

Tests run two actual overlapping tones through scanner, preparer and diarizer
input. Each group keeps only its own frequency and amplitude, and original WAVs
remain byte-identical. Three disjoint spans of one source in the public corpus
still produce one speaker with each inferred item published once. Long legal
UTF-8 source IDs stay inside the existing 256-character speaker limit.
