# D84 review comparisons

`interjections-review.json` contains 120 deterministic cases (Python
`random.Random(292)`) plus two readable-paragraph punctuation controls. Expected
rows were produced by the actual gocassini viewer at
`1111155cb0017b47c072a8662c54f28ad7c44223`, using jiti 2.7.0 and the same pipeline
as `interjections/generate.ts`: portable word/readable/display construction,
validated transcript index, `displaySegmentsForArtifact`, and
`buildTranscriptRows`. No GNOME implementation generated expected rows.

The randomized cases use three speakers, 3–28 uniquely named tokens, 50 ms
start steps, and durations chosen from `[0, 0, 0, 50, 100, 250, 600, 1200]`.
Unique token text makes canonical alignment unambiguous. They exercise zero
length blocks both on their own and mixed with positive timing, nonmonotone
ends, interruption seams, nesting and overlap. Case 041 also checks that a
zero-only block with a positive *envelope* remains audible when rejoined with
another block. A point envelope is not attested, but remains intervening
continuity evidence.

The punctuation controls check joined-text word counts when merging small
blocks and sentence detection across separately stored `.` and `)` items.
Expected rows retain every original atom index. The test compares all row and
member fields except presentation text (as for the original six vectors).
The generator's assertion equating joined block text with space-joined atoms
was omitted for standalone punctuation, which the viewer legitimately joins
without a space; no viewer code was changed.

Generation source, individual input/output fixtures, logs, comparison failures,
and the benchmark are retained on roy at
`/mnt/ai/cassini/dev/evidence/gnome-d84-review-fixes-20261109/`.
