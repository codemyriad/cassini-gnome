# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).
The current pin, `0c9370da` (multistream corpus v2), ships companion 0.2.12, whose browser `track` packets carry
the D61 clock facts (copied into session.json and x["codemyriad.io"].tracks, record-only)
and the span's `documentGeneration` (a live watchdog hint, never written to files). Its
`ok:false` results carry a D58 result `code`, and its native resume packet carries the
command's `requestId`, so a refused resume is keyed to its own ask. It keeps authoritative page-state
publication and a coherent multi-tab inventory. Call identity and the hook
baseline survive worker restarts; persisted addresses use bounded fingerprints.
A worklet must prove its render clock is advancing before source PCM is armed;
per-source readiness failures remain visible while healthy sources keep running.
D33 records explicit end causes
and permits `othersSource.reason: restored` only for a retained browser connection
returning with fresh PCM after fallback; older recordings remain readable.

Run `python3 browser-protocol/verify.py --pinned browser-extension` to verify
both the Git snapshot and extension bytes. Update with
`build-aux/vendor-browser-protocol.sh /path/to/upstream FULL_COMMIT` after the
shared change has landed upstream. Install `browser-protocol/requirements-test.txt`
to run the request-derived stdio conformance suite.

## Browser call lifetime

With D52 multistream capability, a present page with no sources gets a 30-second
others-gone grace only after remote audio has been heard. A missing inventory row
means unknown, and a control reconnect with fresh retained PCM does not select
system sound. A browser-confirmed closed tab keeps the two-second grace.
Navigation or missing page evidence retains D33's 120-second return deadline;
its system fallback begins after two seconds when The others is selected. A
retained browser connection can restore on fresh validated PCM. Call-generation
changes fence the old recording before admitting another call's samples.

See [call-lifecycle.md](call-lifecycle.md) for legacy capability behavior and the
per-capture reconnect, stop and fallback rules.

The private recording harness proves hang-up with a tab left open, reload,
worker reconnection, expiry without an active call, and unaffected simultaneous
meetings. Unit tests advance only the recorded missing timestamp to exercise the
same deadline branches without wall-clock sleeps.


## Source I/O retirement

Merged protocol PR #28 supplies the source-isolation fix pinned by this branch.
An active capture's ended physical span returns `state/source/retired:true`;
its cleanup and in-flight chunks cannot mean the whole capture was saved.
Capture finalization returns `state/command`. Failed spans are fenced locally,
healthy sources and the microphone continue, and a live source retries a new ID.
The pin is protocol commit 0c9370da (PR #41, multistream corpus v2: named `constants`
and a hand-written `expected` block in corpus.json, a third voice, real D49 and seam overlaps,
bleeds both ways, a noisy gapped remote, `captureOutcome`, floor oracle), on top of
protocol commit 032e98fc (the PR #40 merge with Mac's follow-ups, on the PR #39 merge, D61: browser clock facts on `track`,
a span's `documentGeneration` and the departed-generation watchdog rule), on top of
protocol commit 364c87b9 (PR #38, Mac's follow-ups to #37, on the PR #37 merge: the native resume packet carries the ask's `requestId`, a fresh UUID per ask), on top of
the merge commit of protocol PR #36, d789cf30 (the D58 refusal vector follow-ups), on top of
protocol PR #35, 71d9bc4 (D58 classified refusals: the session `refusals` member, optional
`ok:false` result codes and the `conformance/refusals` vectors), on top of protocol PR #34,
95b647aa (D57: the app asks to resume only after the
extension stops reporting the tab's session, `pageState` in the auto-start vectors, the `documentChanged`
watchdog frame for a fast reload's dead spans, and `keepOnNavigation` on the resume Start), on top of
protocol PR #33, 941a82cb, which adds vectors
only (a frame arriving at the 45 s deadline is too late; other-generation rows
drive a lost recording's grace timers) to #32's D52.5 rules (ask-once resume, no
second recording, the 45 s PCM_STALE_AFTER and its watchdog vectors), on top of #31's
heartbeat-v1 and audio transport liveness (D52.4). Companion 0.2.12 answers the
app's control pings (0.2.9), adds the result codes (0.2.10), the resume `requestId` (0.2.11)
and the D61 track fields; see
[call-lifecycle.md](call-lifecycle.md). Conformance
runs give every case a private CASSINI_LIBRARY_DIR, which
tests/conformance_host.py requires.

Selecting a browser category permanently includes already discovered sources,
even one that failed before declaring a track. Rejected selection changes roll
back that expectation. Periodic WAV flush/fsync failures retire only that writer
and checkpoint every healthy writer. Manifest and directory durability errors
remain fatal; they are never downgraded into a per-source response.
