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

Unknown fields are ignored. Unknown host message types are ignored. A missing host uses
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
The extension's hello now advertises `capabilities: ["independent-sessions-v1", "call-evidence-v1"]`.
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
