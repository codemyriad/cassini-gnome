# App/browser control — v1

This separate channel lets Cassini show and record meetings in the browser. It does not
change the audio host, audio SPEC, or the audio vectors. The extension maintains a
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
| code | optional on ok:false, `^[A-Za-z]{1,32}$`; see Result codes |

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
D52.5 adds one exclusion: a call (tabId, callGeneration) with a recording on a
`system/lost` fallback, under any control peer past or present, MUST NOT be
started automatically; it may only be resumed, asked once (see "Control heartbeat").

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
  Extension fallback: if no app Stop has ended a closed tab's retained
  (keepOnNavigation) session 30 s after tabs.onRemoved, while the extension
  worker still runs, the extension finalizes it itself as a Stop with reason
  tabClosed, so neither the capture nor its session slot is held forever. Any
  earlier Stop cancels it. A late page ACK to the closed tab is page transport,
  never a capture failure.
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
statuses retain captureId. Since extension 0.2.12 the same documentGeneration also
rides on each audio-plane `track` of a page span (SPEC.md, "Page trust and browser
clocks"), written by the extension's background; it is a liveness hint only.
Adopting starts echo callGeneration and are checked
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
A failed resume MUST NOT be retried as a fresh Start automatically. When and
how often an app may ask to resume a system/lost fallback (once per loss and
control connection, D52.5) is under "Control heartbeat" below.

```json
{"type":"command","requestId":"join-stand-in","action":"start","mode":"tracks","tabId":2,"independent":true,"callGeneration":"call-A","captureLocal":false,"keepOnNavigation":true,"resumeCapture":{"captureId":"stand-in-F","timelineOriginMs":1793500000000,"lostAtMs":0}}
```

`keepOnNavigation:true` (page-state-v1) applies to a resume Start as to any
independent Start: without it, a reload ends the resumed extension session.

`resumeCapture` requires independent Start, a selected callGeneration, a nonempty
captureId of at most128 UTF-8 bytes and an integer original timelineOriginMs in
0..253402300799000. Unknown members are ignored; no path is supplied. The target
is this recording operation, not merely a call address/generation. The extension
uses its captureId and exact origin from session construction, including status
before the native ACK and every scoped Stop. It does not invent an alias token
or shift timestamps. After asynchronous lookup it checks the selected generation.

`resumeCapture.lostAtMs` (R94): the host MUST send it, a finite nonnegative
number, the `atMs` of the committed othersSource seam it asks to resume: the newest
system/lost (D52.4), or the system/missing stand-in (D49, D58 #2) when that is what
it resumes (`0` in the example above). At send time, after the durable checkpoint completes,
the host MUST copy the value exactly from its committed session state while
holding the capture lock (or equivalent serialization of dispatch with Stop and
capture transitions).
It MUST NOT round it, recompute it from a request/transport timestamp, or guess
an event that is not yet committed. A host without that commit does not ask.

The value identifies which committed loss the ask answers. The live-folder
oracle's O3.resumeAt checks exact reference equality without comparing the request
stamp with the placed loss timestamp: accepted output-time frames can place that
seam later than the commit instant. Equality alone does not independently prove
fsync-before-send ordering; both hosts must test that ordering at their actual
writer boundary (D52.5). The extension does not read it: it neither requires nor
validates `lostAtMs`, ignores it like any unknown member and does not forward it in
the native `resume` packet. Extension 0.2.13 already behaves so; no companion
version change is required by R94.

The first native packet is deliberately a distinct `type:"resume"`, version1,
with independent:true, captureId, timelineOriginMs, callGeneration and features
including resume-capture-v1. It otherwise uses the same transport as Start.
Since extension 0.2.11 it also carries this command's `requestId`, so the app
can tell which ask a refused resume answers. Since extension 0.2.26, ordinary
native Start also carries its originating control command's requestId; see the
verified-admission request binding below.
An app that keys on it gives every resume ask a fresh, never reused requestId
(see conformance/control/RESUME-CAPTURE.md).
Older hosts must reject this unknown type before creating any folder. This is
safer than adding an ignored optional field to Start. The app atomically verifies
that the target is active, not stopping/stopped, the exact origin/generation
matches and the current others source is an eligible system-sound stand-in:
either the initial system/missing stand-in (D49) or a system/lost fallback
after the recording's audio transport closed (D52.4). It
attaches the new transport to that recording. It never creates another folder,
resets its clock or replaces its microphone/selection. Title/microphone defaults
from a fresh Start do not modify the target. A refused resume has no side effect.
The host joins only a committed fallback: the recording's othersSource system/lost
is written once the fallback device delivers audio (its first buffer is admitted),
not when the transport closes. An app MUST wait for that commit before it sends the
resume; one sent earlier is refused and uses up the only ask for that loss on that
connection (D52.5, below).

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

**Return before the fallback commits.** A fallback commits (writes othersSource
`system/lost`) only when its device delivers its first buffer; until then the
recording's source is still `none/lost`, for instance while the macOS
system-audio permission prompt is open. If the call returns in that window
(page present, control alive, a known positive sourceCount, a live audio
transport), the return cancels the pending fallback, and the app MUST restore
the browser source as soon as the returning PCM is ready, whether or not heard
was seen: otherwise the Others would record nothing until Stop. Once the
fallback has committed `system/lost`, restoring still needs heard. The vectors
mark that window with `fallbackCommitted:false` on a live transport. A writer
that wrote none/lost at the loss appends browser/restored; one that kept its
written othersSource appends nothing, since its source never changed. Both leave
the Others absent over the loss (SPEC.md, session-events `uncommitted-return` and
`uncommitted-return-kept`).

**Resuming a lost recording: ask once, never a second recording (D52.5).**
These are app-side rules; the extension is unchanged, and the wire gains only
`resumeCapture.lostAtMs` (R94), which the extension ignores.

- *Ask once.* For a recording on a `system/lost` fallback whose browser's
  hello lists `resume-capture-v1`, the app MUST send one resume-capture-v1
  Start (`resumeCapture` naming that recording) at the first policy tick where
  all of these hold: the recording's othersSource system/lost has been written
  (the fallback committed); the recording's audio transport is closed; control is
  alive; the page is present (pageState `present`, not navigating or closed);
  the call's row is present with a known positive sourceCount; the row's
  callGeneration equals the recording's callGeneration; and heard:true has
  been seen at any time during this recording on a row of that same
  callGeneration (another call's heard never counts, and it never arms this
  recording's restore or OTHERS_GONE_GRACE either).
  The app asks to resume only after the extension's status no longer reports a session for that tab.
  The host joins only a committed fallback: the recording's othersSource system/lost
  is written once the fallback device delivers audio (its first buffer is admitted),
  not when the transport closes. The app MUST wait for that commit before it sends
  the resume; one sent earlier is refused and uses up the only ask for that loss on
  that connection. A fallback that fails before it commits writes no system/lost,
  so the app never asks to resume it. The ask names that commit: its
  `resumeCapture.lostAtMs` is the `atMs` of the system/lost it resumes, exactly as
  the app wrote it (R94, above).
  After that resume is
  refused or fails (error result, refused native resume, missing or
  mismatched proof), and while it is still pending, the app MUST NOT ask
  again on the same control connection: there is no retry on later ticks and
  no retry timer. On each new control connection (a new hello), whose
  predecessor's in-flight commands were discarded, the app MUST ask once
  more, at the first tick where the conditions above hold. A successful
  attach (`restored`) ends the loss; a later fallback to `system/lost` is a
  new loss. This mirrors D49's "ask once" for the initial system/missing
  stand-in, and a failed resume is still never retried as a fresh Start
  (R67, above).
- *Call identity.* callGeneration is a browser-generated, unguessable
  identifier (the extension uses a random UUID per tab call; see "Call
  identity across service-worker restart"), so it cannot collide between
  browsers or calls. Apps MUST match a lost recording to a call by
  (tabId, callGeneration) across all control peers, past and present, never
  by the control peer, connection or hello the recording was started under.
  A frozen companion that returns as a new control peer with a fresh hello
  therefore still finds its lost recording.
- *No second recording.* While a recording of (tabId, callGeneration) is on
  `system/lost`, or has decided that fallback and not committed it yet, that call
  is recording: its row shows Stop (the live
  indicator, not Record), and Record all skips it. The app MUST NOT
  automatically Start that call, even when the `call-evidence-v1` auto-start
  rule would otherwise start it, including after a frozen companion returns
  as a new control peer. It MAY only resume that recording under the
  ask-once rule, so no second folder is created for the call. After the user
  stops that recording, a manual Record is an ordinary independent Start
  with a new identity (R67). A different callGeneration in that tab, or a
  different tab, follows the ordinary auto-start rules.

Literal cases: the `resumeCapture` cases (action `askResume`) in
`conformance/multistream/lifecycle.json` and the `recordings` cases in
`conformance/control/auto-start-vectors.json` (fields `heardSeen`,
`controlPeer`, `expect.resume`), each with an independent oracle
(`lifecycle.py`, `auto_start.py`). The `PCM_STALE_AFTER` watchdog that closes
the transport has its own cases in `conformance/multistream/stale-watchdog.json`.

Shared literal cases: `conformance/control/heartbeat-vectors.json` (ping/loss
timing and the exact wire replies, independent oracle `heartbeat.py`) and the
`transportAlive` cases in `conformance/multistream/lifecycle.json`. Frozen
companion example: control lost at 25 s and computer-sound fallback at 27 s;
the audio transport closed at 45 s (`PCM_STALE_AFTER`); `pageGone` at 145 s if the browser never
returns, or restoration once it returns with a new audio transport.

## Result codes (D58 / R79; extension 0.2.10)

An `ok:false` result MAY carry `code`, a string matching `^[A-Za-z]{1,32}$` as a whole
string (not before a final newline: see SPEC.md, "Refusals"), that classifies the refusal:

```json
{"type":"result","requestId":"record-1","ok":false,"error":"No audio is available from that meeting yet.","code":"noSources"}
```

The field is optional. Hosts MUST accept results without it (older extensions, and Starts
the extension does not classify) and need no capability, since unknown fields are ignored.
A host copies any pattern-valid value, known or not, into the browser refusal's `detail`
(SPEC.md, "Refusals") and drops any other value; the result's `error` becomes its `message`.
This is an open vocabulary, separate from and unrelated to the closed audio-plane `code` of
`response.schema.json` (`version`, `state`, `source`, ...): the extension never forwards a
native host's error code as a result code. Apps do not wait for it: they classify what they
know locally and record a code-less `startRejected` otherwise.

| code | Set by extension 0.2.25 when |
| --- | --- |
| `busy` | a recording is already running or starting, in that tab or (legacy) anywhere; also a legacy Stop for another tab |
| `limit` | 128 requests are pending, or 100 recordings are running or starting |
| `noSources` | the tab has no discovered meeting audio |
| `unsupported` | a reviewed known-mixed site is signal-only and cannot record browser audio (D74) |
| `gone` | the tab no longer exists or closes during Start, the call generation changed before or during Start, or a Stop names a capture that is no longer active |
| `notWebPage` | the tab is not an http(s) page |
| `pageUnreachable` | no page frame of the tab connected in time |
| `invalid` | the command or its options are malformed, or ask for whole-tab capture |
| `resumeRefused` | the host's answer to a resume did not confirm the original capture |
| `workletBlocked` | the page blocked the AudioWorklet (capture.js) |
| `audioBlocked` | the browser kept the page's audio suspended or refused playback (capture.js) |

`busy` comes first: a Start for a tab that is already recording or starting (or, for a
legacy Start, any recording) answers `busy` before any other check, including `noSources`,
`limit` and `invalid`, so an app that falls back on other refusals never records that
meeting twice (D58 #11; wire case `same-tab-busy-result-is-detail-busy`).

Only a Start the app cancelled itself (`Recording could not start.`: its own Stop or
cancel) and a failure reported by the native host, or the loss of that host, carry no code. `workletBlocked` and `audioBlocked` are set where capture.js
raises the error; a tracks Start answers before the page's capture begins, so today these
reach the app as a later source failure (`captureOutcome failed`, P10), not on a result.
Shared wire cases: the `wire` list in `conformance/refusals/writer.json`.

## Tab readiness (`tab-readiness-v1`, extension 0.2.17)

Native control connectivity does not prove that an open meeting tab can reach the
worker. Chromium can leave an old isolated content-script realm behind when the
extension reloads or updates, while its MAIN-world WebRTC hook keeps discovering
sources. An idle browser with no detected meeting is not an error.

A capable hello adds `extensionVersion` (1–80 characters) and `extensionInstance`
(1–128 characters). The instance identifies one loaded extension, persists through
service-worker eviction via `storage.session`, and changes on extension reload,
update or browser restart. The wire version remains 1; old peers may ignore these
optional fields and the new capability.

Each status (including a successful result's nested status) adds `browserHealth`:

```json
{"worker":"ready","nativeControl":"connected","extensionVersion":"0.2.17","extensionInstance":"extension-load-1","state":"reloadRequired","tabs":[{"tabId":2,"state":"reloadRequired","hookSeen":true,"relayAlive":false,"message":"Reload the meeting tab so Cassini can record it.","action":"reloadMeetingTab"}]}
```

- `worker` is `ready`: this status was produced by the worker. Worker loss is
  detected by the app's existing control connection/heartbeat rules.
- `nativeControl` is `connected` or `disconnected`. It describes the persistent
  control port, independently of per-recording native audio connections. The
  disconnected value is observable in the popup; it cannot travel over a missing
  native connection. A host receives status only while its own port exists.
- `tabs` has at most 100 entries, each scoped to this browser/profile's `tabId`.
  It contains only tabs with discovered meeting sources or a lost relay that had
  meeting sources. It does not disclose unrelated idle tab URLs or titles.
- Per-tab `state` is `checking`, `ready`, or `reloadRequired`. `hookSeen` records
  discovery or a valid hook handshake; `relayAlive` records the relay's answer to
  the current instance challenge. A live relay with an incompatible old hook can
  therefore be `reloadRequired` with both booleans true.
- Overall `state` is `reloadRequired` if any detected meeting needs reload, else
  `checking` if any is being checked, else `ready` if there are detected meetings,
  else `idle`. **Idle never produces a reload warning**, even when an unrelated
  restricted page cannot accept injected scripts or the native host is absent.
- Apps render the extension's health decision. Only `reloadRequired` supplies the
  guidance and optional action above. They must not infer a broken meeting from an
  empty inventory, a missing native host, or a tab's URL. An unready tab is not an
  eligible Start/auto-start inventory entry. This decision covers the entire tab:
  one healthy frame cannot make a tab eligible while another detected meeting
  frame is checking, unavailable, or lost. Idle inventory omits that tab, and both
  app and popup track Start refuse with `pageUnreachable` before native Start.
  Admission is rechecked after asynchronous tab lookup/relay discovery. Existing
  recording or pending-Start ownership still takes `busy` precedence. Existing
  recordings retain their inventory, normal status and failure handling; health
  does not fabricate a Stop. Once all detected meeting frames recover, new Start
  eligibility returns; an unrelated unready tab does not block a healthy tab.

The isolated relay and MAIN hook exchange protocol-1 readiness tied to the
worker's current instance. The worker accepts readiness only from its own
extension port and matching instance/protocol. Page evidence remains advisory:
handshakes do not authorize recording, paths, native commands, or app actions.
A relay exists independently of its hook being compatible. A handshake's bounded
deadline reports an unavailable hook rather than treating port existence as ready.

On install, update, startup and control reconnect, the worker probes open web tabs
and re-injects **only the isolated relay** where needed. It does not re-wrap an
already-running MAIN hook. A compatible hook keeps its peer objects, source keys
and document identity. A pre-handshake hook requires a full tab reload. Relay
injection is idempotent; invalidated relays stop, fail admitted/late requests and
remove listeners after capture drains (bounded by ten seconds). Navigation and tab
closure clear stale readiness. A browser may forbid injection into a restricted
frame; failed injection alone is not evidence of a meeting or a reason to warn.
The manifest's `scripting` and HTTP(S) host permissions cover this repair; they do
not authorize recording without the existing popup/app command.

An app may offer an explicit user action by sending:

```json
{"type":"command","requestId":"reload-2","action":"reloadTab","tabId":2,"independent":true}
```

Send it only to a `tab-readiness-v1` peer, only after the user chooses the reload
recovery action. The worker accepts it only for a detected `reloadRequired` tab,
refuses a tab that is recording or starting (`busy`), and invokes Chrome's normal
tab reload. It never starts native capture. A tab without that recovery condition
is refused with `gone`. Deduplication and ordering follow the existing command
rules. The popup provides the same explicit action. Consumers must reconcile the
following snapshots instead of replaying Start on a stale tab instance.

`conformance/control/tab-readiness-vectors.json` supplies consumer states;
`tests/extension-reload.mjs` loads and reloads the unpacked extension, then joins a
persistent meeting page to a synthetic audio peer and verifies non-silent PCM at
the replacement worker. Its native sink is an in-memory fixture, not an app writer
or evidence of device/native-host integration. No microphone or speaker is used.

## Companion diagnostics (extension 0.2.18)

The version/instance hello from `tab-readiness-v1` also carries optional diagnostic
fields `installType` and `filesOk`. These fields do not introduce a new wire
version or capability; **feature gating continues to use capabilities**, never
version strings, install type, or the self-check. Versions are for diagnostic
wording and comparison with an app's bundled companion pin.

```json
{"type":"hello","version":1,"extensionVersion":"0.2.18","extensionInstance":"extension-load-1","installType":"development","filesOk":true,"capabilities":["multi-stream-v1","page-state-v1","call-evidence-v1","resume-capture-v1","heartbeat-v1","tab-readiness-v1"]}
```

- `extensionVersion` comes from `chrome.runtime.getManifest().version`.
- `installType` comes from `chrome.management.getSelf()`, which needs no additional
  management permission. Its values are `admin`, `development` (unpacked),
  `normal`, `sideload`, `other`, or `unknown` when unavailable.
- `filesOk` is true only when startup fetches and reads nonempty `capture.js` and
  `pcm-worklet.js` through this extension's own `runtime.getURL`, without cache.
  A missing/unreadable/empty file or a file-read timeout reports false. This is a
  packaged-file readability check, not a checksum, module execution, audio-device
  test, or proof of per-tab readiness. Unavailable installation metadata leaves
  `installType: "unknown"` independently of the file result.
- Startup bounds the check at 2.5 s, aborting pending file fetches. It completes
  before the first native control connection, so hello is still sent immediately
  on that connection. Each worker checks once and reuses the result across
  control reconnects; worker restart/update performs a fresh check. No retry
  conceals a missing file. `status.browserHealth` and the popup details expose the
  same completed diagnostics; fields can be absent while the popup's startup
  check is pending. A missing file does not accuse an idle browser of a broken
  meeting tab.

Older hellos omit these fields: absence means unknown, not a successful check.
Apps may use diagnostics for setup/recovery wording; they do not change the
existing capability contract or infer that a particular meeting failed.

### Cause of a lost browser capture

An extension Stop that already carries `failureReason: "lost"` also carries an
optional `cause`, next to the existing `problem` text:

```json
{"type":"stop","captureId":"capture-1","failureReason":"lost","cause":"frameError","problem":"The meeting page could not record separate tracks. Record the whole tab instead. Audio recorded so far was saved."}
```

| Cause | Observed extension trigger |
| --- | --- |
| `frameError` | The page hook or offscreen capture reports a fatal error. |
| `frameClosed` | A recording relay disconnects under the existing immediate-loss policy. |
| `tabClosed` | Chrome reports the recording tab removed under that policy. |
| `nativeHostError` | The native host reports a capture-wide error or an audio request fails/times out. Source-scoped refusals retain their existing behavior. |
| `startupError` | An unexpected startup failure occurs after the native folder is owned, outside an explicit cancellation. |
| `transportClosed` | The native audio port disconnects or a native-port write throws. |
| `lifecycle` | The frame stops unexpectedly without a more specific terminal error. |

The first terminal loss cause wins. An earlier nonfatal source warning does not
choose it, and a later failure during cleanup cannot replace it. The extension
chooses causes at its own failure branches; it does not trust a page-supplied
cause or classify failures by parsing human-readable text. The new field changes
neither when a failure is declared nor the existing Stop/fallback policy.

Cause is a bounded ASCII identifier (1–80 characters). Readers tolerate unknown
future codes and absent legacy causes; a cause never changes `failureReason`,
recording state, or end reason. A normal explicit Stop, an opted-in navigation
boundary, or a source warning alone adds neither a lost failureReason nor cause.
If the native pipe is already closed, no Stop diagnostic can be delivered over
it: the app/writer still observes EOF and records its own `transportClosed`
diagnostic. This extension change does not implement app-side persistence or UI.

`conformance/control/companion-diagnostics-vectors.json` contains current and
legacy hellos plus lost/normal Stop examples. Their optional keys remain valid
under the older schemas' unknown-field policy.


## Tab-scoped trusted popup status (extension 0.2.19)

The extension's private runtime RPC supports a browser-owned active-tab ID:

```js
const [tab] = await chrome.tabs.query({active:true, currentWindow:true});
const status = await chrome.runtime.sendMessage({target:"background", type:"status", tabId:tab.id});
```

Only a sender from this extension's exact `popup.html` URL may read this status.
Content scripts, offscreen pages, other extension pages and other extensions gain
no status access. The popup obtains the ID from Chrome, not page-provided data.
A present `tabId` must be a nonnegative safe integer; invalid values return
`{error:"Choose a valid browser tab.", code:"invalid"}`, without another tab's
status or health payload. A well-formed ID with no known capture returns idle;
this lookup does not establish whether that tab still exists or supports capture.

A valid scoped reply always echoes `tabId` and includes `starting`, `recording`,
and the unchanged global `browserHealth` snapshot. The scalar capture fields
belong only to that tab:

- A reserved Start, including a pending explicit-tab lookup, reports
  `starting:true, recording:false`. An untargeted legacy popup Start becomes
  attributable once Chrome resolves its actual tab. An unresolved active-tab
  lookup cannot yet be attributed to a tab.
- A session whose capture setup is not ready, or whose Start is still pending,
  also reports `starting:true, recording:false`. This covers native-start
  acknowledgement, persistence and the remaining capture setup. Consumers must
  suppress reload while starting and must not treat it as confirmed capture.
- Once Start has completed and the session is ready, replies report
  `starting:false, recording:true`, with that session's existing captureId,
  timelineOriginMs, tracks, stopping, failedSources and other status fields.
  Zero tracks means waiting for browser audio; this status does not prove audio
  delivery, app-side recording confirmation or saved audio. `stopping:true`
  remains an in-flight finalization state until the session is removed.
- No current session or pending Start reports
  `starting:false, recording:false, tabId:<requested ID>, path:null, tracks:0,
  stopping:false, failedSources:[]`. It does not substitute the first session or
  a saved path/problem from another or earlier capture. Health remains separate.

For compatibility, omitting `tabId` preserves the legacy `status()` scalar,
including first-session selection and its in-flight `recording` behavior, and
adds no `starting` field. Native app-control status, inventory, capture admission,
reload refusals, relay policy, wire version and capabilities are unchanged.

## Status popup (extension 0.2.20)

The browser companion popup displays active-tab source readiness independently
from the native app-control connection. It has no recording Start/Stop or mode
controls. Apps continue using the existing control commands; the trusted popup
RPC remains available to isolated browser fixtures. This is a presentation
change, with no wire-version, capability, audio, or recording-policy change.

The popup requests scalar status with the browser-owned current `tabId`, so
concurrent captures cannot borrow the first session’s recording state. A pending
Start is shown as preparing, not active capture. A status lookup that does not
answer within four seconds clears previous success wording and reports an
unverified connection; it never automatically repeats a Start or reload.

Only a health row for the browser-owned current tab may provide that tab’s
readiness or reload action. A missing row means no reported detected meeting,
not proof of successful page injection, unsupported audio, or missing permission.
A source is not a participant identity. A native connection, page readiness, or
track count does not prove saved audio. The packaged-file self-check is displayed
as diagnostic information and never overrides the current-tab readiness state.

A user reload warns about leaving the call, uses the existing guarded
`reloadMeetingTab` action, and awaits new evidence rather than claiming detection
or recording succeeded. Rejoin guidance survives source-free refreshes until the
same tab has new health/capture evidence or the selected tab changes. Obsolete
action refusals clear after recovery or a status-fetch failure. Connection details
may be copied without meeting titles,
URLs, or recording paths. Confirmed app recording state, app auto-record policy,
and launching the native app require additional explicit app-backed interfaces;
the popup does not infer or fabricate them.

### Toolbar popup sizing (extension 0.2.21)

The action popup prefers 376 CSS pixels during intrinsic sizing. The root's
percentage maximum and the body's inherited width let it shrink to Chrome's
final containing block, including the smaller CSS viewport at high browser zoom.
It uses no viewport-unit cap that feeds Chrome's provisional viewport back into
its preferred-size calculation, and no hard minimum that overflows at 300% zoom.
There are no protocol or capture-policy changes.


## Peer observation and same-document loading (extension 0.2.22)

Peer registration also observes `getReceivers`, `getSenders` and `getTransceivers`
for libraries that retained a constructor reference. Those getters only register
the peer; they do not create an AudioContext or admit audio. Connected state,
receiving transceiver direction, live audio, inbound RTP progress, call generation
and the existing two-of-three heard votes remain required. DOM streams and local
preview senders do not become automatic call evidence. A previously unknown peer
first observed after an in-page call change is not assigned to the new call by a
read-only getter/stats call; an intercepted new constructor or fresh completed
remote-description negotiation is required. Existing pages need a tab reload to
load the changed MAIN-world observer.

Chrome can emit status-only `tabs.onUpdated` loading events for the same document.
Frames are still invalidated first. An obsolete frame may be restored only when
`chrome.scripting.executeScript` returns current browser metadata matching both
its frameId and its port sender's browser-owned documentId. The script's result,
page-posted documentGeneration, and a connected relay's own claim are not proof.
The check is bounded at 2.5 s and rejects missing/failed metadata, a changed loading
epoch, a disconnected frame or a closed tab. A subsequent browser loading/complete
event or explicit repair may make a new check; no polling retry conceals failure.

After verification the worker requests fresh discovery from that port and restores
main-frame present state. It does not replay cleared source counts, erase call
fences, create a recording or send another native Start. Repair also performs this
check before accepting a connected relay. An unverified old document remains
obsolete; the extension retains its existing fail-closed health/admission policy.
No new browser permission, app wire capability or status API is introduced.


### Talk display-label snapshots (extension 0.2.23)

Eligible Talk receiver tracks may use an untrusted DOM participant-card name,
matched by exact audio-track object identity, as their static registration label.
The native track declaration and source ledger retain labelSource:dom under SPEC's
proposed provenance contract. Ambiguous/missing mappings keep browser-origin labels;
DOM names do not change call/RTP/admission gates or any control/status/health fields.
Product hosts must adopt provenance persistence with the bundled extension; the
private browser-test writer does not establish Swift/Python product adoption.


### Frame-port disconnect diagnostics (extension 0.2.24)

The background frame-port onDisconnect callback reads runtime.lastError before
its existing cleanup/loss handling. Chrome closes extension ports when pages enter
back/forward cache; reading the callback error avoids an unchecked API warning.
This does not classify errors by message text or waive failures: frame removal,
retained-session navigation, ordinary capture loss and fresh relay readiness
continue through their existing paths. No control or native wire fields change.


On a persisted pageshow, the relay replaces any stale cached port through the
same disconnect/drain routine before opening a fresh connection. The content
realm can thaw before its queued disconnect callback; that late callback cannot
stop or retire the replacement port. Pending capture requests still fail/drain,
and a new Start still waits for the old hook's stopped/flush acknowledgement.

## D74 call signals and remote-audio classification

Extension0.2.25 first increment implements connection-scoped negotiation, stable
profile identity, snapshots and the generic trusted-adapter/classification boundary.
The ONLY production descriptor is web.whatsapp.com: mixed design, ALWAYS unknown/
unverified call state and null callGeneration until an approved marker study.
No live WhatsApp support or automatic recording is claimed. Separated/active/end
framework paths are exercised only by trusted extension test fixtures. No AudioNode
hooks, page-audio capture or app policy/timer implementation. Native protocol is
unchanged; old consumers get no callSignals without configure.

D74 supersedes D73's graph-capture proposal. A site with one mixed remote feed
(D74 examples: Teams and WhatsApp Web Audio) is SIGNAL-ONLY: the extension records
no audio from that site. It reports positive call lifecycle evidence; the app
records Others from system audio and its own independently selected microphone.
Sites exposing separate remote streams keep the existing browser RTP capture and
packet/heard policy. No AudioNode hooks, graph observation/tap, mixed browser
capture, new native endpoint/probe or persisted-format amendment is introduced.
D73.12's general open-file-value rule is not reversed. Signal fields are control
metadata; D74.1's app-owned recordingDetails provenance is separate below.
Existing native and session schemas remain unchanged.

### Capability and per-connection snapshots

Proposed capability: `call-signals-v1`, advertised in extension hello only AFTER
implementation. Before consuming new signals, an adopting authenticated control
consumer sends `{"type":"configure","capabilities":["call-signals-v1"]}`.
The extension acknowledges `{"type":"configured","capabilities":["call-signals-v1"]}`
if supported (empty accepted list otherwise). Empty configure revokes that
connection's opt-in. Reconnect begins unconfigured with no replayed eligibility.
This does not change existing heartbeat, status or RTP messages. An old consumer
never receives a new signal-only row through legacy meetings as a shortcut.

Configured consumers receive full replacement snapshots, initially possibly empty:

```json
{"type":"callSignals","browserInstance":"profile-A","sequence":0,"tabs":[{"tabId":2,"url":"https://web.whatsapp.com/","documentGeneration":"document-A","remoteAudio":"mixed","callEvidence":{"state":"unknown","reason":"unverified","adapter":{"name":"whatsapp-signal","version":"fixture-1"},"callGeneration":null}}]}
```

This example is an UNVERIFIED fixture, not a shipped adapter or WhatsApp support.
Sequence is a nonnegative safe integer, strictly increasing per configured control
connection; broker uses that connection only to validate snapshot freshness.
Ignore stale snapshots/frames from superseded connections, reject duplicate tab
rows, and reset sequence on a fresh configured connection. Sequence or transport
incarnation MUST NOT become part of a recording's stable association identity.

`browserInstance` is the stable opaque identity of the browser instance/profile
that owns the tab, at most200 UTF-8 bytes. It must survive control reconnect,
extension worker replacement and updates for the same profile. Use an established
trusted broker profile identity or a profile-local persistent opaque ID, never a
socket/peer handle, connection counter, worker hello.extensionInstance, filesystem
path or page-provided value. Missing/unverifiable identity prevents signal-driven
admission; do not substitute a connection ID. The authenticated current connection
and its sequence establish freshness separately from this stable ownership key.
At most100 tab rows. Titles/URLs/tab IDs are browser-owned; URLs at most2048
characters. Document/call generations are opaque at most200 UTF-8 bytes (schema
characters are an outer bound). Rows are scoped to current browser-verified
documents, never aggregated from an obsolete frame. Subframes cannot override a
top-level site's classification or lend lifecycle proof to another document.

Each row keeps TWO independent facts:

- `remoteAudio`: separated/mixed/unknown. This is a reviewed/versioned site
  evidence source's classification of the remote audio design (generic under D80), not inferred from energy,
  hostname alone, receiver count, labels or a single observed track. Known
  classification requires a named/versioned adapter. Unknown makes no claim that
  a stream is separated or mixed. Classification alone never proves a call.
- `callEvidence`: state unknown/inactive/active, reason, nullable adapter
  `{name,version}` and nullable callGeneration. Active requires reviewed positive call/session evidence (generic under D80),
  a named/versioned algorithm and current call
  generation. Inactive requires affirmative callEnded/nonCall evidence; callEnded
  retains the ended generation so apps stop the correct call. Missing/conflicting/
  unsupported evidence is unknown (unverified/unsupported/evidenceConflict), never
  falsely inactive or healthy. A known mixed design may coexist with unknown call
  state and must remain non-admissible for signal-driven automatic recording.

Known inactive requires an adapter; reason nonCall may have no prior generation.
Document generation changes fence prior evidence. A new call in the same SPA gets
a new call generation; title, audio or unchanged URL cannot preserve the old call.
The schema is site-neutral, not permission to invent/add production site adapters.
D80.1 permits no new hostname-specific adapter, warning or admission rule.
WhatsApp candidate observations remain evidence only; do not implement markers.
Generic algorithm/classification changes require their own review.

### Positive evidence and routing

Page playback, graph presence/energy, a WhatsApp/Teams hostname, voice messages,
status/video/music and their combinations never report active or cause a new
automatic meeting/Start. No PCM, AudioContext, analyser/worklet, graph hook,
extension-initiated getUserMedia/device access, native session or app launch is
used to discover calls. D80 may observe already existing page-captured-track
metadata, without opening a microphone or reading its samples.
Site adapter evaluation and relay hints are bounded, scoped and explicitly
validated; page data remains advisory, never management authority. The allowlisted
new hint interface extends only lifecycle/classification evidence, never page
start/stop commands, paths, browser identity or arbitrary page fields.

Routing and admission are separate from recording state:

| Evidence/classification | App behavior |
| --- | --- |
| active + mixed, verified-auto-admission-v1 acknowledged | Signal-only candidate; app may automatically start system-audio Others ONLY with the acknowledged capability pair, fresh current source alias/background-call binding and the existing auto-record opt-in, permission/readiness and duplicate/Stop suppression rules. No RTP/legacy meeting row is required for this signal-only route; no browser Start |
| inactive or unknown + mixed | No new automatic recording; positive callEnded ends that association and stops the shared automatic recording when its last association ends, even if media still plays |
| active + separated, with verified-auto-admission-v1 acknowledged | Automatic browser Start needs the explicit Chrome/hook/background binding below, current RTP readiness and existing app guards; RTP or a signal alone is insufficient |
| separated legacy/non-adopted path | Existing deployed behavior is unchanged by this proposal; D80.3 accepts the interim exposure until the producer and app adoption land |
| unknown classification | No signal-driven route or automatic Start; never guess browser or system from page audio |

A reviewed known-mixed site is excluded from browser-recordable legacy inventory;
its browser Start is refused with existing result code unsupported before any
browser capture/native session or audio opens, even if the site also exposes one
mixed RTP receiver. Never fabricate zero RTP counters to implement this distinction.
Underlying sourceCount/heard keep their meanings and separate-stream thresholds.
Unclassified sites on the existing RTP path are not silently reclassified by this
SPEC. Adopting apps never use a signal-only row as a legacy browser meeting.
An older app gets neither a new mixed row nor a browser capture silently changed
to system audio. User intent/system capture is owned by the adopting app.

The app owns system capture authorization, source/microphone selection, input
readiness, timeline, loss/gap handling and save confirmation. System audio is a
computer-output mix, not a remote participant stream; no per-person isolation or
mic subtraction promise. App Me stays separately selected under existing policy,
never opened by a browser probe or silently disabled/merged. This draft adds no
new user page-media action, popup controls, graph fields or native protocol.
D74.1 app-owned recordingDetails routing provenance is specified below.
Recording details will explain system routing in one quiet line; exact shared
copy is a separate design task, not implementation text in product UI here.

### Call end, reconnect and app ownership

**D74.1: stable association, transient transport.** Bind pending and running
automatic system recording associations to the EXACT tuple
(browserInstance/profile, tabId, documentGeneration, callGeneration) BEFORE Start
dispatch and revalidate before opening inputs. The control-connection incarnation
is NOT part of this identity. A configured replacement connection with fresh
same-document/same-call evidence reconciles the existing owner; it never creates
a duplicate Start. Late end/active frames from a superseded connection are ignored
before association handling. An end for a superseded document/call generation
cannot end its replacement. Never match by tab alone, title, audio or hostname.

The extension publishes facts, not app timers or capture commands. **App policy**
withdraws eligibility immediately on unknown, row removal, control loss or
navigation, and starts a 120-second monotonic evidence-loss grace for each affected
association whose recording is already running. The grace never authorizes a pending Start after eligibility
is lost: held permission/input completion must revalidate active same-call evidence
and Stop intent before opening inputs. Repeated unknown/removal/loss and
system/microphone audio never reset or extend that deadline. Fresh affirmative active evidence for that SAME stable
association before expiry clears the loss grace and reconciles its owner without
duplicate recording. At expiry the association ends; never resurrect it from
cached active state. Positive current callEnded ends the matching association
AT ONCE, even with media continuing and regardless of the grace. Unknown/loss is
not fabricated positive callEnded. A new call generation fences the old one and
is evaluated as a different call, not an implicit resume/handoff.

**App policy fixes the recording route at Start.** A remoteAudio change within
that call generation (including unknown) cannot switch an existing system owner
to browser capture, create a second automatic recording, or trigger RTP resume/
takeover. Check the existing stable owner BEFORE either signal or RTP admission,
so a newly visible RTP row cannot create a second recording of that same call.
Fresh active same-call evidence can maintain/reconcile the running
association independently of a changed classification; admission of a NEW call
still needs acknowledged verified-auto-admission-v1 plus call-signals-v1, fresh correlated active+mixed evidence with its current non-null source alias, and the existing auto-record opt-in. Separate-stream RTP
policy/recovery remains unchanged for RTP owners. The extension never implements
a 120-second timer, decides microphone policy or commands a system capture here.

**Overlapping mixed calls share ONE signal-owned system recording.** The app
reference-counts stable call associations (including their bounded loss grace).
Ending/expiring one association cannot stop another; finalize when the LAST
association ends/expires, or when the user chooses Stop. No second simultaneous
signal-owned system recording or participant-isolation claim. User Stop suppresses
automatic restart for those associated call generations across reconnect, not a
future genuinely new generation. Unrelated manual recordings/sources keep their
own policies. Existing permission/Stop/shutdown races must cancel pending Starts
before an ended call can open inputs. This intentional system route is neither a
browser-rejected fallback nor a browser loss; browser-loss alerts do not apply.

**App-owned Details provenance (D74.1):** write the ACTUAL Start routing decision
into the recording, not a cache or live-tab inference:
`recordingDetails.othersRoute = {"source":"system","reason":"mixedSiteSignal","site":"<adapter id>"}`.
These are additive OPEN, bounded non-empty strings (1–200 Unicode code points);
preserve unknown values unchanged per D73.12 across cache removal/export/import.
Producers use the defined source/reason and actual adapter id; unknown values
must not fabricate a mixed-site/participant/RTP claim. Show stored provenance using
shared key details.othersRoute.mixedSite as one quiet Details line. Native/session
schemas stay unchanged; this uses the app-owned recordingDetails extension and
is not the superseded page-audio-mix amendment. Adoption includes durable routing
provenance, reference counting, loss grace, fixed route, race handling and shared
copy; a protocol pin alone does not implement these rules.

### Conformance and delivery limits

conformance/control/call-signals-vectors.json contains shape acceptance/refusal,
remote-audio routing, passive-media and synthetic independent call lifecycle
literals. Call end with continued media expects system Stop when the LAST
association ends; opt-out never starts. These are SPEC/schema fixtures, not a
site detector, live WhatsApp claim or implemented app policy. PR75 retains its
real Chromium passive-media negatives unchanged. Future adapter/app PRs need E1
state/event proofs of actual start/end, generation/reconnect/Stop suppression,
no media-only admission, system-source selection and unchanged real RTP/Talk CI.
GNOME/Mac adoption requires deliberate broker/policy/renderer tests, not just a pin.
No extension/native runtime or capability advertisement changes in this draft.

## D80.3: verified generic automatic admission and explicit document binding

This proposed additive amendment separates source observation from automatic
admission. It is not implemented by companion0.2.25. Do not ship a blanket
browser-auto-start guard as an interim fix: fast-track the generic producer/SPEC,
then matched GNOME/Mac adoption, with legitimate Talk positives and passive-media
negatives. Deployed legacy exposure remains explicitly accepted until adoption;
this amendment does not claim to fix old binaries through an ignored new field.

The extension 0.2.26 implements the D80.5 document/frame cohort producer, advertises the verified
capability pair after persistent profile identity is available, and adds the
content-free popup/Copy diagnostics model. The worker reads the private MAIN
collector only through browser-owned script results bound to a current relay
installed at document start. Repaired/older documents remain unverified until a
fresh document loads; manual source capture remains available. Publication uses
new bidirectional progress, bounded reads and per-configuration fencing, with
periodic media-backed refresh/withdrawal under the numeric rules below.

D80.5 supersedes the earlier same-PC-only qualification gate. The measured Talk
publisher/subscriber topology qualifies when its captured sending and received
progress belong to the same current browser document and frame. The producer's
`webrtc-interactive` adapter version `2` denotes this revised rule; the private
native snapshot is version 2 and older snapshot versions cannot grant admission.
No hostname/label registry or SDP/server-identity join supplies evidence or routing.

### Qualification and negotiation

Generic positive evidence requires live remote media on a connected RTC session
AND a live local audio-capture track actually sent by the page in that bound
session. Fresh received-media and captured-sender media progress must both be
observed; connected state, a live/enabled track or an accumulated stats counter
without fresh progress is insufficient. Track provenance must come from the
page's getUserMedia result or a
proven clone, not a permission grant, preview, allocated sendonly slot or synthetic
MediaStreamDestination. No hostname, label, graph, energy, playback or RTP count
can supply the missing capture-sender proof. Observation must not open a microphone.
A listen-only call without an outbound captured sender remains unknown for this
rule, with explicit manual recording preserved.

**D80.5 cohort identity is one browser document and frame, on the current relay
port incarnation.** At least one connected PC in that collector must actually
send a live page-captured microphone/root-or-clone with native send progress;
at least one connected PC there must have live received-media progress in the
same fresh observation window. These may be the same PC or separate PCs. The
window is the existing 2500 ms maximum for BOTH directional progress proofs;
expiry uses the older of the two selected proofs, including browser read latency.
A newly progressing receiver cannot renew an old sender, or vice versa.

The worker binds each protected read to Chrome's exact `documentId` and `frameId`,
the current frame/port object and its epoch, and the existing hook/background
identity ledger below. It never pools facts from separate reads/collectors to
construct a cohort. Cross-document, cross-frame and retired-port combinations
never qualify, even for same-origin frames, a common backend/certificate, or
matching page-chosen labels/SDP/stats identifiers. Current signal publication
remains top-frame-only; authenticated child source facts alone do not admit the
tab. A captured track obtained only in a different frame does not supply this
collector's capture-root provenance.

All observed qualifying peers and captured endpoints in the cohort participate
in the existing native terminal/new-session fence. Losing one direction withdraws
active proof; it is not an affirmative call end. Complete native terminal end
retains the generation for `callEnded`, and the next native episode rotates it.
This rule observes native media without opening a microphone.

**D80.4: active means an interactive captured-media session, not proven answered
human intent.** Echo/microphone-test pages and unanswered early media qualify when
they satisfy R1: live remote progress and an actually sent page-captured mic in
the bound session. These are intentional positives, not detection failures to hide
with labels or hostname exceptions. The app keeps recording visible, manual Stop
available, and the existing end/evidence-loss/ownership rules. This accepted cost
preserves passive-media, preview, synthetic-sender and stale/forged-evidence
negatives. D80.5 intentionally accepts a document that sends a captured mic on
one PC while receiving media on another, even when they are not the same human
call. The formerly negative unrelated-mic/same-backend case is a documented
positive, like echo; a shared backend is neither required nor evidence. Cross-
document and cross-frame combinations remain negative. An unanswered UI alone
neither admits nor denies the rule.

A producer implementing this contract advertises `verified-auto-admission-v1`
alongside `call-signals-v1`. It does not advertise the stricter feature merely
because it can publish D74 signals. An adopting consumer sends both capabilities
only when hello advertises them; configured acknowledges the accepted pair.
Requesting/acknowledging the stricter feature alone is invalid. Empty configure
revokes both; reconnect starts unconfigured and requires fresh acknowledged proof.
The schemas bound configure/configured to these two unique capabilities. Existing
single-feature consumers remain valid. Publish the new binding aliases and the
generic verified signal shape only after the stricter feature is acknowledged on
that control connection; before acknowledgement or after revocation, retain the
legacy negotiated shape and make no verified-admission claim. No new
automaticEligible boolean is added.

The acknowledged pair (`verified-auto-admission-v1` AND `call-signals-v1`) and
fresh correlated callEvidence active are REQUIRED for **every** adopting automatic
Start: the separated browser route, the D74 mixed/system route, and any automatic
native/system fallback after browser rejection. Mixed routing has no D74-only
acknowledgement exception. It supplements,
not replaces, opt-in/application/permission/busy/manual-Stop/transport and route
checks. Revalidate at scheduling, immediately before dispatch, and before any
fallback/input reservation. Unknown, stale or mismatched evidence cannot authorize
pending work. Existing recording ownership, fixed route and120s evidence-loss
grace remain app-owned; unknown is not an affirmative end.

### Document-start trust boundary

The collector claims its immutable reader global FIRST, before checking secure
context, WebRTC, UUID support or any fallible setup. Until complete successful
initialization, the frozen reader returns null. Non-secure contexts and missing
capabilities remain unavailable for generic evidence for the entire document;
page code cannot replace the sentinel or enable it by changing global flags.
Loopback HTTP may be a browser secure context; ordinary HTTP is not.

The native collector runs before page scripts and retains its own native method,
getter, collection-operation, clock and promise references. A later page override
of Object/Reflect, collection prototypes, MediaStream accessors, RTC statistics or
track/transport getters cannot manufacture capture provenance or media progress.
The protected non-configurable reader returns only a null-prototype data snapshot;
MAIN execution reads that protected property directly, without page Object,
globalThis, descriptor or postMessage lookups. Native promise values cannot be
substituted through an inherited page thenable hook. No page-discovery source
count, document generation, call generation or epoch is admission evidence.

Discovery is refused before relay attestation. Readiness bootstraps a native
read before publishing the initial state. For every document-start trusted frame
(including subframes), discovery messages only wake a browser-documentId-bound
native read; the worker adopts identity/source facts
from that read after current-document checks. A proven native terminal session
(closed transports and ended captured tracks) followed by a new native session
changes the collector's hook generation. Only after the trusted read and all
current-document checks does the worker apply its existing callChanged lifecycle:
end a previous recording as callEnded, fence old sources and assign the new
background call generation. There is no page-epoch shortcut around that lifecycle.
Capture scanning and DOM sender deduplication have no access to the collector's
private getUserMedia-root/clone provenance.

The collector strips URL fragments with captured indexOf/slice, without a late
Symbol.split lookup. Its native then calls use a private fixed-species constructor
and restore the API promise's descriptor afterward; a page Promise[Symbol.species]
override cannot supply the constructor used for those observations.

Relays reject synthetic MessageEvents and synthetic pagehide/pageshow events
(isTrusted false), so fake lifecycle events cannot reconnect a live relay.
Repeated ready messages cannot bypass the discovery execution bound. Native window.postMessage
is itself UA-trusted even when called by the page, so isTrusted is NOT evidence
authentication; only the protected native reader provides evidence. Discovery
wakeups have one in-flight read and a 1000 ms per-frame limit, with one trailing
refresh to avoid losing changes. This limits page-triggered execution; normal
verified progress polling remains independent and never renews proof from a
cached discovery message.

Verified meetings inventory includes only native-verified source facts from
current frames, bound by both Chrome documentId and frameId. Native subframe
facts may contribute real sources but never call admission: signal snapshots
remain top-frame-only. If another current frame contributes source facts outside
the qualifying cohort, the producer keeps those facts truthful but reports the
separated capture route unknown rather than widening automatic capture to them.
Page-chosen subframe counts/heard cannot inflate either
inventory shape for a trusted document-start frame. The legacy, unacknowledged
inventory retains its older unverified subframe facts for compatibility. An
unavailable native collector can likewise retain legacy/manual source facts;
they are excluded from verified inventory and do not yield a verified source
alias. A page
can fabricate those legacy facts, and a legacy consumer using them alone can
still misclassify a page before adoption. They are not admission proof. Consumers
must adopt the acknowledged verified contract before using automatic Start; the
real split-PC Talk-positive CI gate still applies before that adoption.

### Native Start request binding

Since extension 0.2.26, every ordinary native `type:"start"` dispatched for a host
control command echoes that command's `requestId`, unchanged in value AND JSON
type, just as native resume already does. This applies to automatic and explicit
manual control commands, on both legacy and verified connections. Zero is a valid
ID; numeric `0` and string `"0"` are distinct. A popup-originated Start with no
host command has no requestId; it must never be inferred to answer a host ask.
No schema change or new native feature flag is needed.

An adopting verified-auto-admission-v1 host gives every Start ask a fresh,
never-reused requestId across automatic/manual asks and control reconnects. It
retains the ask's origin (automatic or explicit manual), connection and bound
browser/hook/background call identity in its own pending/retired-request ledger.
Before reserving any native input or creating an output, match the incoming
native Start to that exact ask; recheck the applicable current authorization and,
for automatic work, fresh verified evidence. The requestId is correlation, not
admission proof. A timeout retires that ask's authorization; a late automatic
Start must be refused without input/output allocation, even if a later explicit
manual ask names the same call. Never join by callGeneration alone, reclassify an
old automatic packet as the newer manual ask, or renew authorization from native
arrival. Unknown/missing IDs cannot satisfy a pending host command; any separate
explicit popup/manual route needs its own authorization. Native and control
transport delivery may be reordered. The shared nativeStartCorrelation vectors
in conformance/control/call-signals-vectors.json cover a later manual packet
arriving before the retired automatic packet for the exact same call.

### Numeric freshness and active-proof renewal

These bounds apply identically to separated/browser and mixed/system automatic
admission. Use monotonic elapsed time locally; the two endpoints do not need a
shared clock. This amendment adds no wire timestamp/TTL field: the producer owns
media-progress verification, and the consumer owns the receipt-time expiry below.

| Bound | Required value |
| --- | --- |
| Maximum age of each direction's native qualifying progress when publishing active | 2500 ms, including asynchronous collection/read delay |
| Maximum interval between media-backed active refresh snapshots while qualifying progress continues | 2000 ms |
| Producer deadline to post unknown after either direction stops progressing | 5000 ms after the older of the last qualifying received-progress and captured-sender-progress times |
| Consumer active-proof expiry | 5000 ms after receipt of the last accepted media-backed active row; expired at elapsed >=5000 ms |

Each active refresh MUST earn fresh qualifying counter progress in BOTH directions
since that row's preceding active publication, with current live tracks/session and
identity rechecked. A newer collector poll/revision, unchanged counter, cached
boolean, increased snapshot sequence, or healthy transport alone is insufficient.
The 2500 ms age check also applies at actual publication after async reads/queues;
those delays cannot reset the proof's age. Missing/hung samples, disabled/ended
tracks or failed identity checks withdraw admission; never refresh from cache.

Every active row included in a full replacement snapshot follows this rule,
including unchanged rows copied alongside another tab's update. Coalesce updates
within the bounds, or publish that row as unknown if it cannot earn a renewal;
never let another tab's activity renew its cached proof. Send renewed active
snapshots with strictly advancing control sequence even when their serialized
admission fields are unchanged; ordinary snapshot deduplication must not suppress
these media-backed renewals. Unknown withdrawal cannot be delayed beyond the
5000 ms producer bound. If the worker/page/transport freezes and cannot withdraw,
the consumer expiry remains authoritative.

A consumer renews the 5000 ms receipt-time lease ONLY from an accepted,
strictly-new-sequence callSignals snapshot carrying that bound active row under
the acknowledged pair. By the producer rule above, such a row represents newly
earned progress, not replayed state. Duplicate/older sequences never renew it.
Heartbeat/ping/pong, hello, configured, status, meetings, sourceCount/heard updates,
PCM traffic, another tab's proof or a timer tick MUST NOT renew active admission.
Clear proof immediately on unknown/inactive, missing row/alias, identity conflict,
supersession, reconnect, revocation or downgrade. Before the first accepted active
row there is no lease, including after capability acknowledgement.

Check this monotonic expiry at scheduling, final Start/write dispatch, and every
native input/fallback reservation boundary. At the exact 5000 ms deadline a
pending automatic Start is ineligible even if pong just arrived. The producer
withdrawal bound and consumer receipt-time expiry are separate guarantees; do not
mistake either for an end-to-end bound from an unshared media timestamp.

Expiry/unknown is loss of new admission, not affirmative callEnded or an immediate
Stop of an existing owner. The existing fixed route/ownership and 120000 ms
recording-evidence-loss grace remain separate. Start that loss handling when proof
is withdrawn/expires; heartbeat/audio cannot extend its existing loss deadline.
Explicit manual recording remains subject to its existing controls, not this
automatic-admission lease. Shared freshness vectors cover exact boundaries,
healthy-heartbeat expiry, cached refresh refusal, media-backed renewal, revocation
and unchanged owner/route behavior.

### Three namespaces and the wire mapping

These identifiers are opaque1..200 UTF-8 bytes and MUST NOT be interchanged:

| Field | Namespace / authority |
| --- | --- |
| callSignals.tabs[].documentGeneration | Chrome top-document ID from the current extension port's sender.documentId, with browser-owned child/parent binding where needed |
| callSignals.tabs[].sourceDocumentGeneration | NEW: current MAIN-hook document alias, bound to that Chrome document by the background; nullable when unavailable |
| meetings[].browserDocumentId | NEW: the same Chrome top-document identity; required for the stricter automatic path, optional for legacy rows |
| meetings[].documentGeneration | Existing MAIN-hook source/span document alias; meaning is unchanged |
| callSignals.tabs[].callEvidence.callGeneration and meetings[].callGeneration | The SAME background-owned call identity, not the hook's local epoch/counter |

The background owns a current ledger associating browserInstance/profile, tab,
Chrome document, current frame/port incarnation, MAIN-hook document/epoch and its
background call generation. It binds actual browser-owned sender metadata and
current collector state before publication. MAIN-world assertions cannot choose
Chrome identity or mint the background identity. Source counts and hook generation0
remain truthful in the passive viewer; its legacy meeting has a background UUID.

Under the stricter acknowledged feature, every signal row includes
sourceDocumentGeneration (null when unknown). Active and callEnded require a
non-null current source alias. Unknown evidence has null evidence.callGeneration;
a pre-existing legacy background UUID alone does not make the call active.
A missing browser/source binding gives unknown admission, never raw-ID fallback.
The schema keeps the aliases optional for older D74 payloads; negotiated consumers
MUST enforce these additional presence/association requirements themselves.

For a separated source row, consumers require all of the following together:

1. Its current authenticated control connection is bound to the signal's stable
   browserInstance/profile, with current configured sequence and no superseded port.
2. Legacy and signal tabId agree.
3. Legacy browserDocumentId equals signal documentGeneration **within the Chrome
   namespace**.
4. Legacy documentGeneration equals signal sourceDocumentGeneration **within the
   MAIN-hook namespace**.
5. Legacy callGeneration equals active evidence.callGeneration **within the
   background-call namespace**.
6. Evidence is fresh/active, capability and current RTP capture readiness permit
   the browser route, and all existing app guards still pass.

For the mixed/system signal-only route, the acknowledged capability pair and
freshness bounds are equally mandatory. Require current profile/connection, tab,
Chrome documentGeneration, non-null sourceDocumentGeneration and the
background-owned evidence.callGeneration from the producer's same ledger. Recheck
that complete binding against the candidate captured for pending dispatch/input
reservation; any changed component invalidates that pending action. A mixed row
may legitimately have no recordable legacy meetings row or RTP readiness. Do not
invent one, require heard/sourceCount for the system route, or bypass the source
alias/strict acknowledgement because the chosen input is native. If legacy
metadata exists for correlation, compare only corresponding namespaces as above.

Never compare legacy documentGeneration directly to D74 documentGeneration.
Neither equality of two raw strings nor a hook epoch matching a background token
establishes a binding. Equal strings without the ledger/current aliases are
insufficient. Conversely, different Chrome/hook strings are normal and valid:

```json
{"type":"meetings","meetings":[{"tabId":2,"title":"Controlled call","url":"https://conference.invalid/session","sourceCount":2,"sourceCountKnown":true,"heard":true,"pageState":"present","browserDocumentId":"chrome-document-A","documentGeneration":"hook-document-A","callGeneration":"call-A"}]}
{"type":"callSignals","browserInstance":"profile-A","sequence":0,"tabs":[{"tabId":2,"url":"https://conference.invalid/session","documentGeneration":"chrome-document-A","sourceDocumentGeneration":"hook-document-A","remoteAudio":"separated","callEvidence":{"state":"active","reason":"activeCall","adapter":{"name":"webrtc-interactive","version":"1"},"callGeneration":"call-A"}}]}
```

The generic adapter descriptor identifies a reviewed algorithm/version, never a
hostname registry. This literal illustrates proposed correlation, not implemented
producer output or vendor support. Subframe proof requires a current browser-owned
parent/top-document binding; it cannot lend another document a sender/receiver.

PC/track replacement, mute and polling do not by themselves mint background call
identities. Proven same-call reconnect/control replacement retains identity;
uncertain continuity withdraws fresh admission without fabricating an end or a
duplicate Start. Navigation/old-frame results and late ends are fenced before
association handling. Persisted identity metadata cannot replay cached active proof.

### Source facts, routing and shared literals

sourceCount/sourceCountKnown/heard remain capture/source facts, including genuine
passive RTP. Do not zero heard, suppress the raw source observation or filter a
magic hook generation to make the negative pass. For adopted separated automatic
routing, the old RTP-only policy is superseded by active+explicit binding+RTP
readiness. An active signal without RTP capture readiness is still insufficient.
Mixed active evidence retains D74's app-owned system route ONLY after the same
strict acknowledgement, fresh proof and current binding checks; it does not permit
browser graph/mixed capture. Unknown classification does not authorize a route.

The shared `separated-existing-rtp-ready` vector now carries a generic active
signal, acknowledged capabilities and unequal Chrome/hook identifiers with an
explicit mapping. Its policy is verified-call-and-RTP, not existing-RTP. Matching
raw cross-namespace strings, missing aliases, a different Chrome/hook/background
call/profile/tab, absent acknowledgement, unknown evidence or opt-out cannot Start
merely because sourceCount/heard are positive. `passive-rtc-viewer` preserves
sourceCount1/heardtrue and hook0 with no captured sender, while expecting unknown
admission, no automatic Start/fallback/input. These are contract literals; actual
producer/consumer boundary and real-Chromium tests are still required.

### Rollout constraint

Do not silently ship the draft's interim blanket/legacy-inventory withholding
behavior: its cost to legitimate Talk and manual inventory requires explicit
coordinator review. Producer/SPEC first, then compatible consumers and a coordinated
release. New consumers require acknowledged matching evidence; old consumers may
ignore fields and retain the accepted legacy exposure until upgraded. A capability
addition alone is not a backwards-compatible security fix. Confirm version pairings,
first publication/pre-ack, reconnect/downgrade and explicit manual behavior in the
consumer review before rollout. Canonical schemas/vectors in this PR do not enable
any producer capability or change current app policy.
