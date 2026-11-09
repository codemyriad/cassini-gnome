# Verified automatic recording admission (D80)

A passive WebRTC viewer can truthfully report received audio without an interactive
captured-media session. GNOME lists those source observations for manual recording.
Automatic recording requires the acknowledged `call-signals-v1` and
`verified-auto-admission-v1` pair, a fresh active row, its explicit identity binding,
and the existing application, opt-in, busy, Stop, permission and transport guards.
D80.4 deliberately accepts echo/microphone tests and early media that satisfy R1;
the consumer does not add a hostname or answered-call heuristic.

The producer verifies progress of both the actual captured sender and remote
receiver in one current document/frame and relay-port cohort. Under D80.5 these
may be different connected peer connections; neither direction crosses documents,
frames or retired ports. The cohort includes same-document echo and early media. GNOME verifies the current control owner and the shared ledger aliases:

| Inventory | Signal | Authority |
| --- | --- | --- |
| current connection/profile and tab | stable browserInstance and tab | authenticated current control connection |
| browserDocumentId | documentGeneration | Chrome document |
| documentGeneration | sourceDocumentGeneration | MAIN-hook document |
| callGeneration | callEvidence.callGeneration | background call identity |

These are different namespaces. Equality of a hook token with a Chrome token is
never a substitute for the published mapping. Mixed/system calls require the same
acknowledgement, current profile/document/source/call binding and freshness, and may
have no legacy RTP inventory. Unknown classification does not pick a route.

Each accepted, strictly newer active row earns a local monotonic five-second lease.
At elapsed **>= 5000 ms** it cannot authorize a new Start or input reservation.
Status, heartbeat, inventory/heard, PCM, stale sequences and another profile's
activity cannot renew it. Unknown/inactive, missing aliases or rows, revocation,
replacement control ownership and identity conflicts withdraw it. The producer's
2500 ms progress age, 2000 ms renewal interval and 5000 ms withdrawal bounds are
producer duties; no shared wall clock or extra wire timestamp is assumed.

GNOME captures the complete binding when it schedules a Start. It checks again at
command insertion, on the actual socket writer after any queued write, at automatic
fallback reservation, after slow metadata work, and at each native input factory
and input start. A changed candidate cannot be silently retargeted. The verified
producer echoes the **exact host requestId on ordinary native Start** (as already
supported for resume); GNOME keeps a bounded host-request ledger, rejects unknown,
cancelled, expired and replayed requests, and checks the bound current proof again.
This echo is the coordinator-approved producer #79 dependency. A popup/manual
Start without a host token stays explicit manual intent, rather than being guessed
from historical call generations.

Startup admission is not retained as a gate on an established recording. A browser
transport loss can still fall back to system audio after reload, worker restart or
disabling auto-record; resuming browser input still requires fresh proof.
A started recording retains its ownership and route. Proof loss starts its separate
120-second recording-loss grace, anchored at the actual lease expiry rather than a
later heartbeat/tick. Unknown and empty source inventory are not affirmative call
ends. A fresh binding for a proven continuing background call can update an
established owner's documents; a pending Start retains its immutable candidate.
An affirmative matching callEnded ends the correct owner. A renewal arriving after
the full loss grace cannot retroactively rescue an expired recording.

With an old 0.2.25 companion, automatic Start and automatic fallback fail closed.
The ordinary source inventory, explicit manual control and setup Done behavior
remain available. Setup explains that automatic detection is unverified, using
`setup.companion.autoUnverified` from the approved shared catalog. The broker's
`automaticVerified` UI field describes capability acknowledgement, not a second
per-call eligibility signal.

## Draft rollout and proofs

The branch pins protocol/producer #79 at
`b2b561bf1dbf51bbd990cb1c78a1d3e0a07c753f`, companion 0.2.26, including
normal native Start request correlation and the D80.5 cohort semantics. The
producer's real Talk split-PC qualification passed before this pin. Consumer
proofs below still exercise actual GNOME Start dispatch and native PCM; producer
qualification alone is not a GNOME automatic-recording proof. D80.3's rejected
blanket guard is not part of this implementation.

The six D80.2 actual-socket regressions retain the frozen real 0.2.25 producer
packets. New tests consume the shared routing/identity/freshness/qualification
vectors, exercise valid separated Starts and D74 system recording with synthetic
inputs, and hold writer/input boundaries across expiry and source changes. Old
positive automatic fixtures now supply explicit synthetic verified evidence; old
source-only fixtures assert the adopted failure-closed behavior. Existing source
and manual/lifecycle vectors are retained. No new skip/xfail, coverage exception,
tolerance or audio golden change is used.

The recording `app-control.mjs` journey retains the 0.2.25 failure-closed
branch for frozen legacy companion tests. With pinned 0.2.26 it uses a real
`getUserMedia` fake-device sender plus received remote tones, then exercises
automatic recording, hidden controls and new-call admission through the native
control writer. Receive-only RTP with a synthetic preview remains a negative
after audible packets arrive; replacing the preview with captured-media sending
permits automatic Start. No synthetic oscillator is relabelled as captured media.

`call-signals.mjs` checks passive audio with the unmodified production collector.
Its mixed-route ownership proof is explicitly a controlled trusted-adapter input
through the production AppControl, Chromium native messaging and GNOME's real
system/microphone PCM path. This tests negotiated mixed-route ownership,
reconnect, Stop suppression, persisted route and call-ended Stop; it does not
claim the generic producer classifies any real site as mixed. The frozen legacy
branch separately retains manual system Record and failure-closed admission.
