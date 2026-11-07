# R67 desktop adapter requirements

`resume-capture-vectors.json` has hand-written operation interleavings and a
literal half-open D49 seam. Extension VM tests run actual management/native
transport code against these identities and schedules, including both status
orders, queued Stop, lookup Stop and delayed native delivery/ACK. Schema tests
validate the packets and the independent placement oracle validates the seam.
These tests do not substitute for each app's actual native registry and PCM.

GNOME and the other desktop adapter additionally run these schedules against a
real retained microphone/system capture (private generated audio):

- Native resume validates the active original identity, origin, selected
  generation and system stand-in eligibility under the same capture lock used
  by Stop. A stopped/changed target never creates a folder.
- Hold either the control status or native attachment; release in each order.
  Identity, origin, folder, existing microphone frames and selection stay equal.
- Stop before session construction, native dispatch and native ACK. No new
  capture appears after the target is saved. Fresh manual Record still works.
- Change A to B while a resume is pending. Late A resume/status/Stop cannot bind
  or finalize B. Native rejection never modifies either capture's PCM.
- A refused native resume answers only the ask whose control requestId it
  carries (extension 0.2.11), and only while that ask is unanswered. A refusal
  without one records nothing on its own: the ask's own ok:false result does,
  if it arrives. A late refusal whose ask is already answered or unknown
  records nothing. A closed control connection records nothing by itself: its
  unanswered ask stays answerable by its own native refusal, never by another
  ask's. An app may forget its oldest unanswered asks past a bound (GNOME keeps
  8); a forgotten ask records nothing. Whichever answer arrives first records
  the ask once; the other adds nothing. The entry is startRejected/resumeRefused
  with the native message, or the reply's code (R82 1b). The app gives every
  resume ask a fresh requestId and never reuses it for another ask, on the same
  or another control connection, after a reconnect or an app restart (a late
  packet can follow a timeout or a closed connection); a UUID per ask is the
  simple way. An app that keys on requestId refuses a resume whose present
  requestId is outside the control requestId domain; one that does not use it
  ignores it (SPEC.md). An app-side timeout forgets the ask: its late native
  refusal then records nothing.
  `askAttribution` in `resume-capture-vectors.json` pins these orders.
- Use seam events/physical tracks exactly as written. Actual playback, export
  and both recognition inputs retain the microphone, use system before6000ms
  and browser from6000ms, without mixing both or dropping the seam sample.

- D52.4: `targets` lists eligible current sources. A system/lost fallback (its
  audio transport closed) is eligible like the D49 system/missing stand-in; its
  seam writes browser/restored instead of browser/callConnected. Run `lostSeam`
  through the same placement path as `seam`.
  The host joins only a committed fallback: the recording's othersSource system/lost
  is written once the fallback device delivers audio (its first buffer is admitted),
  not when the transport closes. An app MUST wait for that commit before it sends
  the resume; one sent earlier is refused and uses up the only ask for that loss on
  that connection. The ask names the commit it follows: `resumeCapture.lostAtMs`, the
  `atMs` of that system/lost (or of the system/missing stand-in) as the app wrote it
  (R94, SPEC-CONTROL.md); the live-folder oracle's O3.resumeAt checks it exactly.
  At dispatch, copy it only after the durable checkpoint, while serialized with
  capture transitions and Stop; equality identifies the loss but does not prove durable-write order.
  Each product host must test at its real writer boundary that a held or failed
  checkpoint sends no ask and that release sends exactly the committed float.
  `D52.5-ask-waits-for-the-committed-fallback` and
  `D52.5-a-new-hello-before-the-commit-waits-for-it` in
  `../multistream/lifecycle.json` pin the order (`fallbackCommitted`).
- D52.5: the app asks to resume a system/lost recording (its audio transport
  closed) once per loss and control connection, once more on each new hello,
  and never auto-starts a second recording of that call, matched by
  (tabId, callGeneration) across all control peers. Timing cases are the `resumeCapture` cases in
  `../multistream/lifecycle.json` (`askResume`); the auto-start exclusion is
  in `auto-start-vectors.json` (`recordings`, `expect.resume`).

Adoption is opt-in. Neither desktop should send this operation before it owns
the native `resume` handler. Extension0.2.7 is not a store upload instruction.
