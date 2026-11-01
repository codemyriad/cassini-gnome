# App/browser control — v1

This separate channel lets Cassini show and record meetings in the browser. It does not
change the audio host, audio SPEC, or the 88 audio vectors. The extension maintains a
persistent Native Messaging connection to **io.codemyriad.cassini.control**; PCM still
uses **io.codemyriad.cassini**, one connection per recording. Either host may be missing
without breaking the other. Losing control never stops or deletes a recording.

## Transport and trust

Use native-endian uint32 byte length + UTF-8 JSON, 1–524288 bytes, exactly framed, stdout
reserved for frames. Objects reject duplicate members and non-JSON numeric tokens. Version
is the integer literal 1. Each control host has its OWN manifest and explicit allowed_origins
(no wildcard); installing the audio host does not authorize a control origin. The native
host/broker keeps browser connections distinct: tabId is scoped to one browser/profile,
not globally unique. The broker must namespace peers and route commands to the right peer.

A local broker uses a user-only Unix socket (0600 in a 0700 directory; GNOME's default is
$XDG_RUNTIME_DIR/cassini/browser.sock), or equivalently protected IPC. Validate ownership
and peer identity; do not expose a TCP listener. Production broker IPC is platform-owned.
Conformance uses an isolated socket with NDJSON envelopes and CASSINI_CONTROL_SOCKET;
a host launcher may adapt its private IPC to this test interface. It must not use or touch
the user's running app/socket, desktop, tabs, devices or recordings.

**Page content may report counts only, never titles/URLs, start/stop, or paths.** Background
gets tab metadata from Chrome. A page's reports are advisory discovery hints, not verified
participant identities or permission to record. Audio commands retain the existing token
and request checks. An untrusted page cannot send management commands through its relay.

## Messages

Extension → host, immediately on each connection, in this order:

- `{"type":"hello","version":1}`
- `{"type":"meetings","meetings":[{"tabId":2,"title":"Team call","url":"https://meet.google.com/example","sourceCount":3}]}`
- `{"type":"status","recording":false,"tabId":null,"path":null,"message":"Ready. Choose Separate tracks or Whole tab.","tracks":0,"stopping":false}`

Meetings/status are complete replacement snapshots, also sent when state changes. The initial
list may be empty while page discovery answers; later snapshots fill it. Entries aggregate
live frames by tab. Zero discovery removes an idle candidate; a recording row remains even
with zero sources. Navigation/closed tabs/removed frames clear stale counts. Titles/URLs
come only from tabs.get. No audio is captured for discovery. Reconnect sends hello and fresh
snapshots, not a fabricated stop or a replayed start. App policy must reconcile that status
before auto-starting. Snapshot delivery may coalesce; PCM packets never enter this channel.

Host → extension:

```json
{"type":"command","requestId":"record-1","action":"start","tabId":2,"mode":"tracks"}
{"type":"command","requestId":"stop-1","action":"stop","tabId":2}
```

Start resolves the explicit tabId with tabs.get; it MUST NOT use the active tab. It requires
a currently discovered source. Only `mode: "tracks"` is accepted from the app. **Whole tab
requires a click in the browser** (activeTab); the app cannot obtain that invocation grant.
Stop is scoped to tabId: it cannot stop another tab's recording. An idle stop is idempotent.
The popup and app share one recording. A second start in either place is refused with the
same exact message: **A recording is already running. Stop it before starting another.**
The app/broker coordinates its own native capture ownership as well.

Extension → host:

```json
{"type":"result","requestId":"record-1","ok":true,"status":{"type":"status","recording":true,"tabId":2,"path":"/recordings/Recording-example","message":"Recording separate tracks · 1 recorded track","tracks":1,"stopping":false}}
{"type":"result","requestId":"bad-1","ok":false,"error":"Choose a meeting to record."}
```

Legacy commands execute in arrival order. Independent-capability commands use
the per-tab ordering described below. A valid requestId always receives a result while connected;
an invalid command gets ok:false with an error, and no audio side effect. Missing/invalid
requestIds cannot be correlated and are ignored. Duplicate IDs on the same connection reuse
the original result, even while pending. Remember up to 128 requests, evicting only completed
oldest results; never re-execute a pending duplicate. Deduplication is connection-local; after
reconnect the broker must reconcile the fresh status, not blindly resend a start. A disconnected
peer's queued commands are discarded; an already executing command may complete, and its
recording continues. No management message supplies a filesystem destination.

| Field | Type / limit |
| --- | --- |
| requestId | nonempty string, ≤128 Unicode code points, or nonnegative safe integer |
| tabId | nonnegative safe integer; null only in status when unknown |
| meetings | ≤100 entries, active row first; sourceCount 0–4096 |
| title / url | strings ≤200 / 2048 code points; web URLs only |
| status | recording bool, tabId nullable, path nullable string, message string; optional tracks integer ≥0 and stopping bool |
| action / mode | start or stop; start requires tracks |
| error | string ≤2000 code points |

Unknown fields are ignored. Unknown host message types are ignored. Apps ignore unknown
extension message types. The extension sends no unsolicited message types beyond those
listed; new types are replies to app-initiated, capability-gated requests (for example
`pong`, see heartbeat-v1). A missing host uses
bounded 1–30 second retry plus a one-minute alarm that survives MV3 worker idling; do not use
Chrome-150-only alarm persistence options. Permissions added: tabs (background metadata),
alarms (reconnect). The minimum remains Chrome 116.

## Page discovery interface (GNOME owns page-hook.js)

Background/relay sends `{channel:"cassini-control",type:"discover"}` independent of any
recording token. Page hook answers and publishes count or evidence changes with
`window.postMessage({channel:"cassini-page",type:"discovery",sourceCount:N,heard:B}, "*")`.
N is an integer 0–64 per document. Count distinct live audio receiver tracks in
recvonly/sendrecv transceivers on connected peers (connectionState connected or ICE
connected/completed). Do not count senders: a preview microphone is not call evidence.
Muted receivers still count. Discovery creates no AudioContext, capture or native session.

`heard` is scoped to the peer connections whose receivers supplied packet evidence.
Once each second, call `receiver.getStats()` for eligible receivers. A tick votes
only when an inbound-rtp audio report's `packetsReceived` rose by at least **10**
since that receiver/report id's last known counter. A first appearance establishes
its baseline and never votes (even with a positive counter). Missing reports keep
their prior baseline. Reports must carry a string id and a nonnegative safe-integer
counter. Failed, missing, outbound and video reports do not vote. Two votes in the
last three ticks set the latch; only peers still connected may contribute those votes.
Forced discovery snapshots never sample. Stats calls have a 750 ms deadline;
late reports, overlapping stale samples and receivers no longer eligible cannot vote.

The latch belongs to the peers that voted. When the last owner closes or leaves
the connected state, start a **five-second monotonic grace**. A new peer inherits
only after its own qualifying vote within that grace; until then its receivers
publish heard false. An original owner reconnecting inside the grace retains its
latch. A stored grace deadline is checked before considering connected owners:
at or after that deadline, even a reconnect observed before the next periodic
snapshot must start unheard. Without a proven handoff/reconnection, expire the latch, counter baselines
and tick history. Thereafter a new baseline and two qualifying ticks are required.
Keep sampling during a latch to observe new peers. Silence on a connected owner
with live receivers does not clear it. The existing **ten-second monotonic
sourceCount-zero reset** also clears the latch and all sampling history.

Relay validates count and normalises heard to `=== true`, forwarding only
`{type:"discovery",sourceCount:N,heard:B}`. On pagehide it clears cached count/heard
to 0/false and publishes that empty state; late discovery while hidden is ignored.
Pageshow and worker reconnect force discovery, with no cached replay before the
hook answers. The relay never forwards page start/stop/management payloads.
Background includes `heard` in each meetings entry: true if any contributing frame in that
tab has positive sourceCount and a true latch. An idle frame cannot lend its latch to a
different live document. Publish transitions even if the count did not change.

The extension advertises `call-evidence-v1` in hello capabilities (alongside any other
capabilities). For that capability, apps auto-start only when sourceCount is positive and
heard is true. A missing/false heard is insufficient. Legacy companions without this
capability retain their previous auto-start behavior. Listing, manual Record, Record-all,
and all end-of-call/fallback rules are unchanged. A listed pre-join candidate may be
recorded manually. This evidence is advisory and must not be presented as a participant count.
The version-1 control envelope is unchanged; heard is optional for legacy readers. Shared
[`auto-start-vectors.json`](conformance/control/auto-start-vectors.json) inputs and
hand-written expectations must run against each desktop's production auto/manual
start policy; see [adapter assumptions](conformance/control/AUTO-START.md).

## Product behavior shared by both apps

Show **Meetings in your browser** under the Record control, with Chrome title and service if
recognized. Each row has **Record**; while recording it becomes the live indicator. Never
expose tab IDs, frames, sources or tracks in app UI.

Automatic recording waits for a remote participant's audio. If the user is alone,
or everyone else is muted, it does not start automatically; manual Record still
works. Packet progress is evidence of received audio, not speech recognition or
participant counting. The ten-packet floor rejects isolated/low-rate DTX packets;
continuous comfort noise or hold music may still qualify.

Auto-record is OFF by default. Settings label: **Record every browser meeting automatically**.
Explain once that future browser meetings will be recorded locally, and show a notification
with **Stop** for every auto-start. This policy belongs in the app, not in the extension.
The companion connects automatically: **Listening inside your browser**. The app starts
only separate-tracks mode. Whole tab requires its browser click.

Two UX points are awaiting Silvio's decision after GNOME supplied counterexamples: converting
sourceCount into a people count (mixed receiver slots and sender/receiver pairs are not
people), and automatically broadening a selected browser meeting to computer/microphone
capture on failure. These are not control-wire semantics. This transport never estimates
participant identities or initiates desktop capture. The interim app wording can be
**Audio available** / **no audio yet**, with an explicit **Record this computer** action;
if native capture is selected, its indicator is **Recording this computer’s sound**.
Native permissions and consent apply. Both apps must adopt the same resolved product choice.

Mac follow-up after this PR: the same app-side list/Record/live indicator, protected local
broker/control host with its own allowlist, opt-in Settings switch, notification with Stop,
and native fallback. GNOME owns its broker/UI and page discovery in this rollout.

## Verification without a desktop

`node --test extension/tests/*.test.cjs` runs actual background/relay/control code in VM
fixtures; control cases are in conformance/control/vectors.json. The host runner is
`python3 conformance/control/run.py HOST_OR_TEST_LAUNCHER`: it creates isolated protected
IPC and verifies both directions against those same envelopes. Neither runner launches
Chrome, the app UI, microphones or other audio devices. The existing audio runner remains
unchanged and runs alongside them with a headless host fixture in shared CI; app CI also
checks the real native audio host. Schemas live in schemas/control-{extension,host}.schema.json.

Chrome API basis: [Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging),
[tabs metadata permissions](https://developer.chrome.com/docs/extensions/reference/api/tabs),
[alarms and pre-150 persistence](https://developer.chrome.com/docs/extensions/reference/api/alarms).


## Extension identity

`extension/manifest.json` pins the extension's public `key`, so the extension ID is the same however it is
installed (unpacked, self-hosted or from a store):

    pcafdaahfhjiolfpfnlhmljfgoabbaij

- Native host manifests written by Cassini (per user, never system-wide) list exactly
  `chrome-extension://pcafdaahfhjiolfpfnlhmljfgoabbaij/` in `allowed_origins`. There is no wildcard and no manual ID entry.
- The matching private key is held by the project owner outside every repository. It is needed only to sign a
  packaged extension or to keep this ID on a first store upload. Never commit it.
- Tests that load the extension unpacked get this ID too. Tests may still read the ID at run time.


## Independent sessions capability (D17 / D18)

This additive capability preserves all v1 messages and legacy vectors. Without
`independent: true`, the popup/app start retains the v1 single-slot refusal.
The extension's hello now advertises `capabilities: ["independent-sessions-v1", "call-evidence-v1", "page-state-v1", "multi-stream-v1", "resume-capture-v1", "heartbeat-v1"]`.
Only apps that see that capability may request concurrent capture.

An app may send `start` with `independent: true` for each active-call tab. Each
recording has its own native audio connection, token, source clock, frames and
persistent recovery entry. A pending start reserves its own tab; it cannot block
another tab. Independent commands for the same tab execute in arrival order;
commands for different tabs may execute and return results concurrently. A legacy
command waits for preceding independent commands, and later commands wait for that
legacy command. Request IDs still correlate every result, including out-of-order
results from different tabs. A valid independent Stop marks that tab's pending start cancelled
immediately, while its result still follows the start result in arrival order.
It cannot create a late host after Stop; invalid/duplicate stops have no new
side effects. Starting the same tab twice is refused. There are at most 100 live
or pending sessions per extension. `stop` with `independent: true` targets only
its explicit tabId; an idle stop is idempotent and never stops another tab.
Losing one tab/PCM host cannot stop a different session. Control reconnect still
leaves all PCM sessions alive and publishes replacement snapshots.

Every status adds `sessions`, a complete replacement list of the live recordings,
including those saving. Each entry has recording/tabId/path/message/tracks/stopping/captureId
as in v1 (path may be null while start is pending). Command result's top-level
status is for that tab; the aggregate snapshot's top-level fields retain the first
live recording or the last saved status for legacy readers. Consumers must use
`sessions` when present, even when it is empty. Recovery persists all session
paths and opaque capture IDs under `cassiniSessions`; `cassiniActive` retains its first-entry legacy
shape. A terminated worker reports every saved path as interrupted and never
claims that PCM capture survived termination. The status adds an `interrupted`
list of those saved statuses after recovery; `sessions` still contains only live captures.

Independent starts may include `captureLocal` (default true) and `microphone`
(default true). `captureLocal: false` excludes page sender/microphone-preview
sources. Apps that supply their own per-session microphone copy set it false,
so that copy is not duplicated by the page sender. The background passes the
microphone choice to the native host in its start message, along with
`independent: true`, `captureId` (the background token, an opaque string of
at most128 characters) and `timelineOriginMs` (its Date.now() session origin), which
lets the platform's own microphone share the source timeline. These optional
start fields do not change the version1 PCM transport; legacy hosts ignore them.
No page controls the choice or supplies an output path. Invalid option types are
refused before any audio side effect.

When an independent recording stops because its page/PCM path was lost, its stop
message adds `failureReason: "lost"`; an explicit Stop omits it. The platform can
use this distinction for Q4 system-sound fallback in the same folder. This field
does not itself capture a system source and never selects a microphone fallback.

Capture reads source tracks before the tab's output. A remote media-element clone
of a receiver is the same source and must not create a second recording track;
known clone roots are deduplicated. Source IDs still describe capture sources,
not participant identities. Whole-tab capture still requires the browser action
invocation grant and keeps its single offscreen document.

## Call lifecycle capability (D33; extension 0.2.3)

Hello also advertises `page-state-v1`. Version remains1; independent transport
and `call-evidence-v1` auto-start rules are unchanged. Both new and legacy tab
statuses may add `pageState`: `present`, `closed`, or `navigating`. It appears in
per-tab `sessions` entries, the tab-scoped command result/top-level status and
meeting entries. It is optional for old peers. An absent field is unknown,
not evidence of a closed tab. The existing `sessions` replacement-list semantics
are unchanged; there is no new inventory of stopped sessions.

Only the browser's `tabs.onRemoved` establishes `closed`. Document loading and
trusted top-frame relay pagehide establish `navigating`; a fresh top-frame relay
or pageshow establishes `present`. Same-document URL changes do not invalidate
healthy audio. Obsolete document messages cannot revive old counts or override
closed, and a subframe cannot assert top-level lifecycle. A missing control peer
or native port is unknown loss, never an inferred closed tab. Title/URL still
come from the tabs API, never page-supplied lifecycle data.

Extension 0.2.3 delivers the lifecycle publication fix to local installs of
0.2.2. D39 installers replace an installed companion only with a strictly newer
version; changed extension bytes must not retain the version of an earlier
published bundle.

For an active recording, after the initial `hello`, `meetings`, `status`
snapshot, publish each lifecycle transition immediately on the connected
control port, before waiting for an asynchronous meeting inventory. Until that
initial snapshot is sent, transitions are represented by its current state,
not by an early standalone `status` that would break connection ordering.
A rapid `navigating` → `present` transition must not be coalesced away by a
pending tabs lookup. The publisher first awaits **all** metadata lookups in the
batch, then synchronously collects source count, heard evidence and page state
for **every** row from the current documents, with no intervening await. Reading
A's evidence when A's lookup resolves is insufficient: A can reload while B's
lookup is pending. An unloaded document's count must never publish after its
navigation/return statuses. Missing tabs and invalid metadata remain omitted.
A disconnected control port still uses the ordinary replacement snapshot on
reconnect; historical transitions are not replayed as new events.

Before evaluating lifecycle policy, a `status` reporting `navigating` or `closed`
invalidates the cached source count and heard evidence for **that tab** (effective
`sourceCount: 0`, `heard: false`). This applies both to per-tab `sessions` entries
and to a tab-scoped top-level status. A later `status(present)` cannot revive the
departed document's count. Only a subsequent `meetings` snapshot supplies fresh
source evidence; other tabs' counts remain intact. In particular, the wire order
`meetings(present, 3)` → `status(navigating)` → `status(present)` →
`meetings(present, 0)` keeps the navigation return deadline and must not start the
short call-ended timer. `conformance/control/call-lifecycle-wire-vectors.json`
contains literal packet/tick interleavings, effective cached evidence and actions
for both apps to run, alongside the existing normalized lifecycle vectors.

The corpus also records the faulty-publisher trace `status(navigating)` →
`status(present)` → `meetings(present, 3)` → `meetings(present, 0)`. Without a wire
freshness marker, a reader cannot distinguish that stale positive snapshot from
real returned audio followed by hang-up: the positive count clears the return
deadline and zero sources then start the two-second call-ended timer. Its literal
expected actions document why the publisher must avoid this trace when no new
sources appeared. Readers must not mask it by ignoring valid positive snapshots.
The two-tab blocked-lookup extension regression checks the actual publisher:
after navigation/return, both its pending inventory and dirty refresh report A's
current zero count even while B's metadata lookup had held the batch open.

An app that implements this policy opts in for an independent start with
`keepOnNavigation:true`. The extension retains that recording's native connection,
capture ID, origin and folder when frames unload, and starts returning frames
with the original token/origin. An opted-in closed tab also remains owned until
the app's Stop. Without this flag, existing immediate loss/`failureReason:lost`
behavior is preserved for older apps; the popup's legacy start is unchanged.
The flag requires `independent:true` and action=start. It does not recover a
terminated extension worker or dead native connection; their existing interrupted
recovery status remains truthful. An app never switches to an absent PCM transport.

An app Stop may carry `reason`: user/callEnded/tabClosed/pageGone/disk/error/appQuit.
It is validated before cancellation or any recording side effect. On an opted-in
session it is forwarded in the native stop packet; absent means user. Repeated
Stops share one finalization and never emit a second end event. A transient lost
path that keeps a fallback is not a terminal Stop and must not write end.

The app owns these monotonic per-session timers (seconds):

- `CALL_END_GRACE = 2`: present page with zero sources, outside a pending reload,
  stops as callEnded. Recovery before2s cancels that pending end.
- Browser-confirmed closed tab stops as tabClosed after2s. It never starts a
  computer-sound fallback. Closed is terminal evidence despite late messages.
- Navigating/absent page starts computer-sound fallback after2s in the same folder.
  `BROWSER_RETURN_GRACE = 120`, measured from first loss, bounds its lifetime.
  Present with zero sources during that pending return does not become callEnded.
  Once the same tab has returned with audio, a later hang-up uses the normal2s
  callEnded grace even if restoring the selected browser source failed; keeping
  the working system source is not itself evidence of a new navigation.
- Check a stored120s deadline **before** accepting a returned page, including a
  return after expiry before the next policy tick. Expiry stops as pageGone.
- Same browser instance and tab returning with positive sourceCount and a live
  original PCM transport before expiry can restore the browser path. Under
  D52.4 this means a live PCM transport, original or resumed through
  resume-capture-v1 after the original closed. Do not use
  a different tab, title or URL as identity. Respect the current per-session mic
  and The others choices; The others off stays off. Keep the computer source if
  a staged restoration fails; do not checkpoint a restored event without its track.
- Brief trouble that leaves the page and sources present changes nothing.

Timers and source changes are independent for concurrent recordings. These rules
never split the folder or broaden a recording to the microphone. Track/source
identity, rollback, 64-track limits and atomic event/track checkpoints remain in
force. `conformance/control/call-lifecycle-vectors.json` supplies literal timed
expectations for both apps, checked by an independent fixture oracle. It is not
production policy code. Each app must additionally test its actual writer and
fallback/restore device failures.

## D52 multi-stream lifecycle (extension 0.2.6)

The extension advertises multi-stream-v1 alongside its existing capabilities.
The following policy supersedes the legacy two-second present-page call-end
rule only for adopting apps: after remote audio has been heard in this call,
a present page with a known zero source count has OTHERS_GONE_GRACE=30 seconds.
A 15-second rejoin survives; a call that never supplied remote audio does not
arm this timer. A closed tab retains its two-second grace. Missing inventory is
unknown, never zero. Active rows retain their last browser-owned title/URL when
lookup fails and mark sourceCountKnown:false; the cached count is not evidence.
Healthy admitted browser PCM prevents fallback for absent control. The native
microphone alone is not that evidence. Shared literal cases are in
conformance/multistream/lifecycle.json. How apps derive controlAlive and
transportAlive from the wire is in "Control heartbeat" below (D52.4).

Rows/status include optional documentGeneration and callGeneration; capture
statuses retain captureId. Adopting starts echo callGeneration and are checked
after tab lookup. Stops echo captureId and are checked before cancellation.
A changed call address retires the old token before admitting new-call audio;
fragment-only navigation preserves the existing call. Document-local generation
changes likewise fence old spans. Reloading the same address retains the
browser-owned call generation. Old peers closing late cannot open B under A.
Peer roots retain their call binding across stats reads and local sender changes.
A reused peer is rebound only after a successful remote description whose API
call began in the new generation; a negotiation begun in A and completed in B
cannot rebind it. Deliberate call fencing emits trackEnd(callChanged), never a
source failure or retry into the new call. Fresh RTP baseline/progress is required before reopening its
remote spans. Sender/media aliases belonging to a fenced peer stay excluded.

Live sources retry indefinitely with monotonic deadlines: 1, 5, 15, then 30-second
backoff, resetting after 60 seconds of healthy acknowledged audio (zero samples
are healthy). Initial attach failures retry too. Remote spans require a connected
peer and positive inbound RTP progress, not merely recvonly slots. A suspension
that would create a gap above 60 seconds rotates the span before sending it.
The background never forwards a 65th distinct track ID; a refusal stays per
source and is visible in failedSources. See the schemas and new source tests.


### Call identity across service-worker restart (D52)

A browser service-worker restart is not a new call. Persist the per-tab browser
callGeneration and a bounded SHA-256 address fingerprint in storage.session,
with the main frame's hook
documentGeneration/callGeneration baseline. Hydrate these before processing page
messages or browser URL changes and before publishing the initial inventory.
Hydration compares the current browser-owned tab address to the saved digest;
long query strings cannot discard the identity or expand the stored record. A
URL boundary fences the saved hook baseline before the first returning snapshot,
so stale heard evidence cannot vote or rotate the generation a second time.
A reconnect from the same live document and hook generation keeps the same
browser callGeneration. A reload at the same URL also retains call identity;
a real address or same-document hook-generation boundary still rotates exactly
once, in either event order, including while startup hydration is pending.
Store only bounded tab metadata, never PCM or source labels, and retain these
identities independently of the active-recording records removed after Stop.


### Source readiness failures during retry

A source that cannot initialize its render clock keeps its specific error and
sourceTrackId while retrying. Live status includes that source in failedSources
and keeps the error visible when another source declares a track or sends PCM.
Readiness has a four-second upper bound; a timeout disconnects the unsuccessful
graph without closing a shared context or stopping healthy sources. Repeated
identical retry errors need not generate repeated warnings.

Only acknowledged PCM from that same source clears its active retry warning;
a successful declaration alone is not recovery. Permanent/previously recorded
source failures remain in the capture ledger. A source still retrying at Stop
is reported failed, with its existing specific error retained. A recovered
transient readiness error does not turn the final Stop into an interruption.

## Resume an app's active stand-in (D52.1 / R67; extension 0.2.7)

Opt-in capability `resume-capture-v1` joins an already recording native system-
sound stand-in. Apps MUST see this hello capability before sending the command;
an unknown optional field on an ordinary Start is not safe feature negotiation.
Fresh manual Record remains an ordinary independent Start with a new identity.
A failed resume MUST NOT be retried as a fresh Start automatically.

```json
{"type":"command","requestId":"join-stand-in","action":"start","mode":"tracks","tabId":2,"independent":true,"callGeneration":"call-A","captureLocal":false,"resumeCapture":{"captureId":"stand-in-F","timelineOriginMs":1793500000000}}
```

`resumeCapture` requires independent Start, a selected callGeneration, a nonempty
captureId of at most128 UTF-8 bytes and an integer original timelineOriginMs in
0..253402300799000. Unknown members are ignored; no path is supplied. The target
is this recording operation, not merely a call address/generation. The extension
uses its captureId and exact origin from session construction, including status
before the native ACK and every scoped Stop. It does not invent an alias token
or shift timestamps. After asynchronous lookup it checks the selected generation.

The first native packet is deliberately a distinct `type:"resume"`, version1,
with independent:true, captureId, timelineOriginMs, callGeneration and features
including resume-capture-v1. It otherwise uses the same transport as Start.
Older hosts must reject this unknown type before creating any folder. This is
safer than adding an ignored optional field to Start. The app atomically verifies
that the target is active, not stopping/stopped, the exact origin/generation
matches and the current others source is an eligible system-sound stand-in:
either the initial system/missing stand-in (D49) or a system/lost fallback
after the recording's audio transport closed (D52.4). It
attaches the new transport to that recording. It never creates another folder,
resets its clock or replaces its microphone/selection. Title/microphone defaults
from a fresh Start do not modify the target. A refused resume has no side effect.

```json
{"type":"started","path":"/Library/.sessions/Recording-example","features":["source-outcomes-v1","resume-capture-v1"],"resumed":true,"captureId":"stand-in-F","timelineOriginMs":1793500000000,"callGeneration":"call-A"}
```

The extension requires this explicit proof, including the exact identity/origin/
generation, before admitting tracks. A missing/mismatched proof closes only the
attempted transport; it never sends Stop to an unaccepted target. A native host
must not let that failed transport's EOF/Stop finalize an unrelated active job.
Once attached, unexpected EOF keeps native recording/fallback under D33; an
ordinary user Stop finalizes normally.

Stop is fenced at receipt, not only when its queued handler runs: it cancels a
matching queued resume, a pending tab lookup and a session awaiting native ACK.
The app must also reject a resume arriving after it stopped the native target,
including when Start/Stop cross between the independent control/audio pipes.
This cancellation does not prohibit a subsequent fresh manual Record for the
same call. Changed-call signals from A must not bind or stop B.

On successful attachment, transition othersSource from system to browser at the
committed seam, retaining microphone and selection. The seam reason is
callConnected when resuming the initial system/missing stand-in (D49), and
restored when resuming a system/lost fallback (D52.4). Literal seams for both
are in resume-capture-vectors.json (`seam`, `lostSeam`).
S-6 ownership is half-open: system audio before the seam and browser audio from
the seam, with no overlap or dropped sample. Stored audio is not filtered.

Early status carries timelineOriginMs (also on each sessions[] entry). A pending
status is an intent, not native acceptance: apps switch sources only after their
own native attachment has committed, regardless of control/status delivery order.

## Control heartbeat (heartbeat-v1, D52.4 / P11; extension 0.2.9)

A frozen browser or companion can keep both native connections open, so "the
connection is still open" is not liveness. Opt-in control capability
`heartbeat-v1` lets the app detect a silent control link. The direction is
fixed: **the app pings, the extension only answers.**

```json
{"type":"ping","seq":17}
{"type":"pong","seq":17}
```

- The app sends `ping` only when the connection's hello lists `heartbeat-v1`,
  and only after it has received that connection's first `status` (this keeps
  the hello → meetings → status order). `seq` is an integer JSON number in
  0..9007199254740991 (an integral value such as `3.0` is the integer 3, as in
  JavaScript and JSON Schema), starting at 0 on each connection and increasing
  by one per ping. The pong echoes its numeric value.
- The extension answers `{"type":"pong","seq":N}` immediately, echoing seq
  exactly, from its control receive handler: never through the command queues,
  which may legitimately hold a command for longer than the loss deadline
  (tab lookup, Stop flush, PCM acknowledgement). The pong does not wait for the
  extension's own initial snapshot. A ping with a missing or invalid seq (not a
  nonnegative safe integer) gets no reply, like an uncorrelatable requestId.
  Unknown fields are ignored. A ping has no audio or command side effect and
  takes no request slot; it resets the extension's reconnect backoff as a
  command does.
- The extension never sends `pong`, or any other new type, unprompted: an older
  broker may close the link on an unknown extension message type.

The app owns these monotonic timers for each connection (milliseconds in the vectors):

| Constant | Value | Rule |
| --- | --- | --- |
| `HEARTBEAT_INTERVAL` | 10 s | Send a ping when `now - lastPingSent >= 10 s`, measured from the first status and then from each ping. Pings are unconditional, not idle-only. |
| `CONTROL_LOSS_AFTER` | 25 s | Declare control lost when `now - lastInboundFrame >= 25 s`, where every inbound frame counts (pong, meetings, status, result, hello, or any other). Never earlier; SHOULD be within one policy tick. |

The loss deadline runs only while heartbeat is active, i.e. after a negotiated
hello and its first status. A hello without `heartbeat-v1` is never pinged and
never timed out; an open connection remains its only liveness signal. Apps gate
on the capability, never on the extension version number.

On loss the app closes that control connection without waiting behind a stuck
write, and proceeds exactly as for EOF: `controlAlive` becomes false for every
recording of that browser and in-flight commands are discarded. Losing control
still never stops or deletes a recording; with healthy browser PCM (`pcmLive`,
see SPEC.md "Audio transport liveness") nothing changes. The extension notices
the closed relay through its ordinary disconnect handling and reconnects with
a fresh hello and snapshot; seq restarts at 0. There is no extension-side
heartbeat timer. Apps bound writes to the browser at `WRITE_DEADLINE` = 10 s
and close a peer that stops reading (SPEC.md).

**Audio transport lost while the call is visibly ongoing (D52.4 Q3).** When a
recording's audio transport closes (EOF, error, or the app's
`PCM_STALE_AFTER` watchdog) the app falls back to computer sound at once. If the
page is present with a known positive sourceCount and control alive, the app
stays on that fallback until the call ends: it arms no missing timer and no
`pageGone` deadline. A later known zero count after heard still stops as
callEnded after OTHERS_GONE_GRACE. Restoring the browser source needs a live
audio transport: a system/lost fallback is an eligible resume-capture-v1
target, and that seam writes othersSource browser with reason `restored`
(`callConnected` is only for resuming the initial system/missing stand-in,
D49). If control is lost too, the ordinary
missing/BROWSER_RETURN_GRACE rules apply from the moment of control loss.

Shared literal cases: `conformance/control/heartbeat-vectors.json` (ping/loss
timing and the exact wire replies, independent oracle `heartbeat.py`) and the
`transportAlive` cases in `conformance/multistream/lifecycle.json`. Frozen
companion example: control lost at 25 s and computer-sound fallback at 27 s;
the audio transport closed at 40 s; `pageGone` at 145 s if the browser never
returns, or restoration once it returns with a new audio transport.
