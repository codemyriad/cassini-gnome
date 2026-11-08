# Verified automatic recording admission (D80)

A passive WebRTC viewer can truthfully report received audio without an interactive
captured-media session. GNOME lists those source observations for manual recording.
Automatic recording requires the acknowledged `call-signals-v1` and
`verified-auto-admission-v1` pair, a fresh active row, its explicit identity binding,
and the existing application, opt-in, busy, Stop, permission and transport guards.
D80.4 deliberately accepts echo/microphone tests and early media that satisfy R1;
the consumer does not add a hostname or answered-call heuristic.

The producer verifies progress of both the actual captured sender and remote
receiver. GNOME verifies the current control owner and the shared ledger aliases:

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

The initial branch pins SPEC/schema/shared vectors at protocol
`a8efd93e29578962c78fe05e06d62b3693956944`; its bundled producer is still 0.2.25.
Do not release this intermediate pairing. Pin producer #79 / companion 0.2.26 only
when merged, including its normal-native-Start request echo, and run real producer
and app proofs before marking the PR ready. D80.3's rejected blanket guard is not
part of this implementation.

The six D80.2 actual-socket regressions retain the frozen real 0.2.25 producer
packets. New tests consume the shared routing/identity/freshness/qualification
vectors, exercise valid separated Starts and D74 system recording with synthetic
inputs, and hold writer/input boundaries across expiry and source changes. Old
positive automatic fixtures now supply explicit synthetic verified evidence; old
source-only fixtures assert the adopted failure-closed behavior. Existing source
and manual/lifecycle vectors are retained. No new skip/xfail, coverage exception,
tolerance or audio golden change is used.
