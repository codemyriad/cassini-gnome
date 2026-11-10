# Corpus regression: physical overlap is an interjection

Input: the 295 actual words stored by `accept_all` in
`test_corpus_pipelines.py`, session 05200000-0000-4000-8000-000000000001.
They were captured from the existing test fixture, not reconstructed by hand.

Expected rows: the unmodified shared `generate.ts` pipeline importing the real
viewer at gocassini `1111155cb0017b47c072a8662c54f28ad7c44223`, using jiti 2.7.0
(the version pinned in that source's lockfile). The copied generator was run
outside the repository; no GNOME renderer generated these expectations.

The viewer's `buildTurnModel` independently reports:

- source A host: 48440–50000 ms, 1440 ms audible;
- source B interjection: 49076–49996 ms, 720 ms audible;
- their intersection: **644 ms**, kind **interjection**.

Thus this is credible overlap, above 150 ms, displayed as a chip rather than an
`over` badge. The viewer's row has `over: []` and members A / chip B / A.
`corpus-classification.json` is the viewer's model diagnostic, including its
actual intersection intervals.

Fast tests compare the whole 12-row portable projection and this exact row with
GNOME's additional D63 source-gap information. The existing pipeline corpus
check also compares its actual stored row roles to this independent expectation,
retaining its existing acoustic timestamp tolerances. The frozen-word tests
compare every timestamp and original word index exactly.

`source-gaps.json` preserves the fixture's extra D63 input. These gaps currently
split two other GNOME rows (14 total versus the viewer's 12); the 49–50 s row is
identical with or without them. This difference is recorded, not hidden by the
selected-row assertion or presented as full D63/viewer parity.
