# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).
The current pin, `4a59e973`, ships companion 0.2.9 with authoritative page-state
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
The pin is the head of protocol PR #32, 4a59e973 (D52.5: ask-once resume,
no second recording, the 45 s PCM_STALE_AFTER and its watchdog vectors), on
top of #31's heartbeat-v1 and audio transport liveness (D52.4). It is
re-vendored at #32's merge commit once merged; the extension is unchanged at
companion 0.2.9, which answers the app's control pings; see
[call-lifecycle.md](call-lifecycle.md). Conformance
runs give every case a private CASSINI_LIBRARY_DIR, which
tests/conformance_host.py requires.

Selecting a browser category permanently includes already discovered sources,
even one that failed before declaring a track. Rejected selection changes roll
back that expectation. Periodic WAV flush/fsync failures retire only that writer
and checkpoint every healthy writer. Manifest and directory durability errors
remain fatal; they are never downgraded into a per-source response.
