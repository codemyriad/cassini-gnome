# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).
The current pin, `01772b8`, ships companion 0.2.27 with browser-sourced `Me` capture on
separated WebRTC calls (`captureLocal: true` on `start` and `resumeCapture`, sent only to a
companion advertising `sender-me-v1`; an older companion keeps the app's own microphone for `Me`),
one `Me` source per microphone across peer connections, sender mute judged on the render clock,
`getDisplayMedia` display-share audio exclusion (track and stream clones) from `local` sender and
`media` preview capture, a reload-required answer for a page hook older than that exclusion, and
updated D68
`live-folder` and D58 `conformance/refusals` vectors: separated WebRTC calls never open or fall
back `Me` to a raw OS microphone, accepting `Me` gaps across page reloads rather than capturing
unmuted hardware audio while muted in the meeting. Earlier pins include companion 0.2.20 (`85ce233`) with the tab-scoped status popup
(#67/#68). It retains invalidated-relay recovery (#64), extension-owned health and loss-cause diagnostics
(#65), and the English UTF-8 relay sample (#66). It also includes the committed
Stop clock placement fix (#63). Native D69 writer adoption is merged in main;
app-side health presentation remains separate work. The previous pin, `8a3f856c` (protocol main with PR #61, D61.1: steady drift follows the capture clock; on #56: a capture context re-anchors after a render stall; on #55, D68: the live-folder driver's monotonic session clock; on #54, D68.1: every capture AudioContext keeps real time; on #45 and #44: a return before the fallback commits restores at once; either writer form, with the absence in the file), shipped companion 0.2.15, whose capture contexts feed an inaudible constant (6e-8) to their output so Chromium never moves a silent capture graph onto a slow timer, re-anchor forward after a render stall so later chunks are not placed early, and whose browser `track` packets carry
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
The pin is protocol main 85ce233 (#67/#68, companion 0.2.20), on f537d9ce
(#64–#66, companion 0.2.18), on cf9beaa (#63,
committed Stop clock placement), on b8a493a (#62, D69/D69.1 contract), on 8a3f856c (PR #61, D61.1: steady capture-clock drift is followed
without spurious gap steps, while confirmed stalls re-anchor forward; companion 0.2.15),
on e3724c6 (PR #60: a failed/retired span releases its mute hold), on 4451bf0 (PR #58:
the live-folder driver maps the session origin at its own instant), on cf04e024 (PR #56, D61.1: a render stall re-anchors every span of its
capture context forward from the stalled frame, judged on fresh getOutputTimestamp levels; the
host sees only an offset jump and writes its `jump` gap; companion 0.2.14), on ab2823c2 (PR #55,
D68: the live-folder driver times every request and observation on the monotonic clock, its
session origin mapped once, and the control relay stamps each frame on it too), on 54e0006b (PR
#54, D68.1: an inaudible 6e-8 ConstantSource feeds each capture AudioContext's output, so Chromium's silent-sink suspender never moves it onto a slow timer;
captured PCM is unchanged; companion 0.2.13), on e68ebca (PR #53: the live-folder driver reads a
process's exact argv, `/proc` on Linux and `KERN_PROCARGS2` on macOS, and a host runs its whole
command as consecutive arguments), on 98b3513 (PR #52, the shared test 2 live-folder driver, with
#49's live-folder oracle, #48's presence from the D55 placement and #47's placement golden), on
protocol commit 854f4ada (PR #45: the uncommitted-return vector accepts GNOME's kept
othersSource as well as Mac's none/lost then browser/restored, both with the Others absent over the
loss), on protocol commit dba4a327 (PR #44, Mac #134: a call that returns before its fallback
commits restores at once, heard or not; GNOME keeps its written othersSource and writes nothing),
on protocol commit c0284a6f (PR #43, Mac #113: the host joins only a committed
fallback, whose othersSource system/lost is written once the fallback device admits its first
buffer, so the app asks to resume only after that commit, on every control connection; the
`fallbackCommitted` lifecycle step field and two D52.5 cases; an empty gap entry, `durationMs`
and `frames` both 0, is valid and is no range, a mixed one is invalid), on top of protocol
commit 981fb4d5 (PR #42, the D63 corpus vectors: `activeSeconds` bands,
`personActiveSeconds`, `fallback`, `turnCases` and `wordMidpoints` in corpus.json, with
fallback.json, writedown.json and their oracles), on top of protocol commit e6d39987 (PR #41, multistream corpus v2: named `constants`
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
heartbeat-v1 and audio transport liveness (D52.4). Companion 0.2.18 answers the
app's control pings (0.2.9), adds the result codes (0.2.10), the resume `requestId` (0.2.11),
the D61 track fields (0.2.12), real-time capture contexts (D68.1, 0.2.13) and the render-stall
re-anchor (D61.1, 0.2.14) and steady-drift handling (D61.1, 0.2.15); see
[call-lifecycle.md](call-lifecycle.md). Conformance
runs give every case a private CASSINI_LIBRARY_DIR, which
tests/conformance_host.py requires.

Selecting a browser category permanently includes already discovered sources,
even one that failed before declaring a track. Rejected selection changes roll
back that expectation. Periodic WAV flush/fsync failures retire only that writer
and checkpoint every healthy writer. Manifest and directory durability errors
remain fatal; they are never downgraded into a per-source response.

### Broker build identity and idle upgrades

Status and the internal audio-ready reply carry the broker's immutable payload
identity (app, version, build ID, module root, PID and start time). Clients compare
it with the current installed payload; Flatpak clients consult the host's current
deployment while the broker reports the build it actually loaded. Legacy audio
ready replies remain accepted and marked as legacy.

A mismatched or identity-less broker is retired only after an idle status. New
brokers recheck their identity and unfinished captures/recording operations under
the same lock as Start, then accept an idle shutdown. Legacy retirement checks the
socket peer UID and exact Python module/socket command line, rereads status and
uses a pidfd to send SIGINT without a PID-reuse race. Unknown or busy peers remain
running. The client waits for the old service lock before spawning its replacement.
SIGTERM takes the broker's normal capture-finalization path. BuildRefresh performs
its own idle recheck before restarting the app.

### Browser loss evidence

A lost Stop keeps its optional bounded ASCII `cause` and the first 1,000
characters of its `problem` on the committed `othersSource` event as `cause` and
`detail`. Unknown future identifiers remain opaque; malformed diagnostic causes
are ignored. The fields do not alter fallback, selection or Stop policy. An audio
transport EOF records `transportClosed`. Cleanup after an earlier loss does not
replace that loss's evidence, and a normal explicit Stop adds no loss evidence.

## Extension health in GNOME (D72)

The broker projects each reconciled control peer’s optional hello diagnostics and
`status.browserHealth` without inferring meeting readiness. Setup completes only
when connected profiles support multi-stream capture and none explicitly reports
`filesOk: false`. Missing, pending or malformed self-check diagnostics remain
unknown and do not block Done or request file repair. Only explicit false shows
the files-missing line. Version comparison is diagnostic wording; recording
remains capability-gated.
Only an extension-reported, non-idle `reloadRequired` tab gets reload guidance.
The explicit app `reload-tab` request is scoped by `browserId` and `tabId`; the
broker rechecks capability, health and busy state before sending `reloadTab`.
Acknowledgement does not mark a tab healthy: fresh extension status must do that.
Details renders a saved loss cause only for a committed browser-to-system fallback;
legacy, unknown and lifecycle causes use the catalog’s neutral `other` wording.
