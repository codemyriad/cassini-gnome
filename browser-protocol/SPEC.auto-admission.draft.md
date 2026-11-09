# Draft: verified automatic admission, separate from source observation

**Historical design proposal; canonical SPEC-CONTROL.md D80.3–D80.5 is controlling.**
The original failure receipt uses companion0.2.25. Companion0.2.26 implements the
same-document/frame interactive-media cohort producer and verified negotiation; consumer adoption
follows separately. The superseded withholding proposal is not an implementation
requirement. No hostname-specific markers or graph capture are introduced.

## Confirmed failure at the consumer Start boundary

An actual Chromium0.2.25 passive WebRTC viewer received a generated440 Hz stream with RMS0.2482050933764155, no getUserMedia/local microphone and no call controls. The shipped MAIN hook truthfully reported sourceCount1/heardtrue. This alone did not prove automatic recording.

A second isolated test now establishes the missing boundary:

1. The shipped0.2.25 worker/control code, run in its existing VM harness, projects those facts at a neutral origin (`https://media.invalid/viewer`) into legacy `meetings`: sourceCount1, sourceCountKnowntrue, heardtrue, pageStatepresent. It opens no real native host.
2. That serialized hello/inventory is fed into actual GNOME0.4.2 release code, commit `a03252df5336f8d9ed7117cd31d4c12191f269ba`, with auto-record enabled and an explicitly simulated application present. The call-signals handshake is also acknowledged; it is not omitted to manufacture legacy behavior.
3. Production `policy` → `_automatic` → `command` sends a real control packet to `BrowserConnection.send`: `action:start`, `mode:tracks`, `captureLocal:false`, `microphone:true`. Only the final wire sink is mocked; it acknowledges recordingfalse. Native fallback/input is forbidden and zero captures are created.
4. With auto-record off, no command is sent. A desired no-Start regression fails at the final packet assertion on the released code, not at setup/imports.

This proves an **automatic Start request**, not actual microphone/device opening or a saved recording. Browser RTP source observation, worker inventory projection, and consumer admission are separate stages. All must be covered; the browser observation is not to be falsified to make the regression pass.

GNOME's `_auto_start_ready()` currently accepts positive known sourceCount plus heardtrue for a `call-evidence-v1` companion. That capability's legacy name must not be treated as proof of an independently observed call: heard is still received-media evidence. `call-signals-v1` negotiation alone does not strengthen that legacy predicate.

## Independent Meet source-observation receipt

The supplied 2026-11-08 two-browser Meet study used companion0.2.25 with observer Chrome for Testing149.0.7827.55 and actor Chrome156.0.8078.47. After manually verified joins, the observer had one connected/ICE-connected PC and three live recvonly audio tracks; inventory was sourceCount3/heardfalse. Speech made a previously muted receiver unmuted and moved the fresh inventory to heardtrue, with increasing RTP/sample counters. Leaving the observer produced a fresh empty inventory.

This is evidence of received-source discovery, observed audibility and retirement in that tested flow. Three receivers are **not proof of three participants**; active-speaker slots remain an unproven hypothesis. The observer's inputs were denied, and a sendonly/live/muted slot is not proof of an actual page-captured microphone on a sender. No native recording host, Start command or PCM capture was used. Independent call admission, per-participant separation and saved recording remain unvalidated. This Meet result is separate from the isolated GNOME automatic-Start-request proof above; neither may be substituted for the other.

The generic contract must preserve those source facts without promoting heardtrue to active evidence, must not infer a new logical call from receiver/PC changes, and must not treat an empty inventory alone as an affirmative user-call end. A listen-only receiver can have genuine media while failing a mic-sender candidate: that is unknown automatic admission, not unavailable source capture. Add a literal regression for heardfalse → heardtrue → fresh empty inventory with admission still unknown unless independent evidence exists; maintain no automatic Start/fallback/input and no per-person claim. Automated guest-join/codec failures remain a separate precondition issue, not an extension failure.

Receipts: coordination `REPLIES-EXTENSION-meet-observations-2026-11-08.md` and `evidence/extension/meet-observations-2026-11-08.json` (committed at623e9ed). They contain sanitized lifecycle/counter facts; do not copy room URLs, user identities, labels or media into protocol diagnostics. No Meet-specific selector or hostname rule is proposed.

## Proposed minimal semantic change

Keep existing sourceCount/sourceCountKnown/heard truthful and preserve explicit manual Start/record-all semantics. Require fresh **independent verified call/session evidence** for every automatic Start, including a system fallback following browser rejection. Missing, unknown, ambiguous or stale evidence never implicitly qualifies. Do not derive it from hostname, energy, playback, graph presence or receiver count. D80.4 accepts R1 as an interactive captured-media session, not proven answered human intent: echo/microphone tests and early media with a sent captured mic are intentional positives. D80.5 accepts split PCs within the same fresh browser document/frame/current port; the unrelated-mic case in that cohort is an intentional positive. Passive/preview/synthetic, cross-document/cross-frame and stale-evidence negatives remain strict. The contract does not itself implement the producer.

Use the existing `callSignals` evidence vocabulary, identity/fencing and separate remoteAudio classification where possible, rather than duplicate the same facts in an automaticEligible boolean with a different lifetime. A generic detector may carry a stable algorithm identifier/version in the existing adapter descriptor; it must not be a hostname registry or a page assertion. Whether that descriptor can be reused verbatim must be checked by GNOME and Mac consumers before amendment.

Automatic browser capture requires all existing opt-in/application/permissions/Stop/busy/transport guards **plus** fresh correlated active evidence and browser-supported separated-source capability. Mixed active evidence keeps D74's app-owned system route; unknown/unavailable capability is not permission to invent a route. This proposal does not prove participant separation from one/multiple receivers.

Correlate browser/profile, tab, current browser document and call generation. Document and generation encodings must be consistent between legacy inventory and the evidence stream; never join by URL/title or assume unrelated generation counters are interchangeable. Evidence updates and commands can race: check admission at scheduling, immediately before wire send, and immediately before any native-fallback/input reservation. Stale ends or async replies cannot admit/end a replacement call. Existing app policy owns the evidence-loss grace and fixed recording route.

**Producer refusal alone is insufficient.** GNOME0.4.2 may respond to a browser Start rejection by opening a native fallback. Suppressing an unsupported Start in the extension without fixing consumer admission can therefore still produce an unwanted recording. No production fallback test opens a device for this work; the regression's factory is forbidden.

## Compatibility policy — older withholding proposal superseded by D80.3

Adding an optional admission field to a legacy meeting is insufficient: existing consumers ignore it and still start from heardtrue. Redefining heard to mean call-active silently corrupts the source observation contract. Existing behavior cannot be made safe for every old consumer merely by an unknown extra field.

Proposed additive negotiation, **name provisional**: `verified-auto-admission-v1`, carried by the existing hello/configure/configured capability mechanism alongside `call-signals-v1`. No new message type or public automatic Start flag is needed merely to express the guard. Proposed canonical schema/vector additions are now included for review; their runtime adoption still requires consumer/coordinator review.

**D80.3 is controlling.** The older mandatory legacy-inventory withholding and
pre-ACK first-publication suppression proposal is superseded, not an
alternative requirement for implementation. Retain truthful legacy/manual
inventory and the negotiated legacy shape; accepted old-consumer exposure remains
until matched adoption. Any future withholding requires separate explicit review.

| Pairing | Controlling D80.3 behavior |
| --- | --- |
| New consumer + new producer, both capabilities acknowledged | Consumer may list source observations, but every separated/browser or mixed/system automatic Start needs fresh correlated active evidence and the current source alias. Preserve manual source-based actions. |
| New consumer + old producer, or missing acknowledgement/evidence | Automatic Start/fallback fails closed as unverified; manual workflows remain explicit. UI must disclose that automatic call detection is not verified instead of silently promising it. This intentionally tightens old auto behavior. |
| Old consumer + new producer | Preserve truthful legacy inventory/manual workflows and legacy negotiated shape before/without the stricter ACK. The old consumer retains the explicitly accepted legacy exposure. No silent withholding or blanket guard is authorized. |
| Old consumer + old producer | The released failure remains; document the need for the reviewed producer/consumer update, not a claim that this draft fixes deployed binaries. |

**Superseded first-publication requirement:** the earlier instruction to withhold
unverified legacy rows starting with the first publication, before ACK, is NOT a
D80.3 requirement. Before/without stricter ACK, keep the compatible legacy shape
and truthful inventory. An adopting new consumer independently refuses all new
automatic Starts until both capabilities are acknowledged and fresh correlated
proof arrives; it preserves explicit manual/status/control workflows. Old consumers
retain the accepted exposure. Do not confuse capability-gated new fields with
blanket legacy-inventory suppression.

The canonical [D80.3 section](SPEC-CONTROL.md#d803-verified-generic-automatic-admission-and-explicit-document-binding)
sets the numeric bounds: active proof age at publication <=2500 ms, active refresh
interval <=2000 ms while qualifying progress continues, producer unknown withdrawal
within5000 ms of either direction losing progress, and consumer expiry at5000 ms
since the last accepted media-backed active row. Heartbeat/pong/status/inventory
cannot renew proof. Every adopting automatic Start, including mixed/system and
native fallback, requires the acknowledged capability pair and current non-null
source alias. See the canonical rule for full-snapshot renewals and existing-owner
grace; this draft does not override it.

D80.3 rejects an interim blanket consumer guard: fast-track the generic
producer/SPEC, then matched consumer adoption and boundary regressions. No
capability advertisement or runtime change is enabled by this contract PR.

## Required literal cases / actual boundary regressions

| Case | Source observation | Independent evidence | Expected automatic boundary |
| --- | --- | --- | --- |
| Passive real RTP viewer | One source, heardtrue, healthy transport | Unknown, no interactive call proof | No browser Start and no native fallback/input; raw source facts retained |
| WebAudio/AudioWorklet or ordinary media-only | Graph/media activity may be real | Unknown/non-call | No automatic Start, no graph browser capture |
| Reviewed generic interactive separated session | Actual independent sources and delivery evidence | Fresh active with matching identity | One eligible browser Start, subject to existing app guards |
| Active mixed session | Honest mixed capability; no legacy/RTP row required | Both capabilities acknowledged; fresh active with non-null source alias/current binding | No browser Start; app-owned system route only under these verified gates and existing policy |
| Active queued, then unknown/end/navigation | Source may still play | Admission withdrawn before dispatch | Cancel queued auto Start and fallback reservation |
| Browser refuses while evidence becomes unknown | Source possibly present | Admission no longer valid | No native fallback/input |
| Old document/generation active or late end | Source facts belong elsewhere | Does not match current association | No Start or termination of a newer recording |
| Explicit manual request with unverified call | Actual source is available | Unknown | Preserve reviewed manual semantics; do not implicitly apply auto policy |
| Auto-record off or no required app | Any | Even active | No automatic Start |
| Reconnect/replayed evidence | Same current identity | Same valid call | No duplicate automatic Start; manual Stop suppression retained |

Test the real consumer dispatch through its final wire/input boundary with private transports and forbidden device factories. For generic positives, separately prove the detector against representative actual-site flows; synthetic fixtures alone do not establish site support. Keep real passive RTP plus media-only and cross-document/cross-frame negatives, plus the documented same-document split-PC positives, with no hostname exceptions. Require Mac and GNOME consumer review, then agy/coordinator review, before merging any schema/policy/runtime change.

The concrete identity mapping and updated separated-existing-rtp-ready/passive-rtc-viewer literals are in the D80.3 section of SPEC-CONTROL.md and conformance/control/call-signals-vectors.json. Chrome documentGeneration and MAIN-hook documentGeneration are distinct namespaces; use the explicit browserDocumentId/sourceDocumentGeneration aliases and the shared background callGeneration, never raw cross-namespace equality.
