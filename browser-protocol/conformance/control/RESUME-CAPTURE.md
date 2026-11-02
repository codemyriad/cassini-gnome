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
- Use seam events/physical tracks exactly as written. Actual playback, export
  and both recognition inputs retain the microphone, use system before6000ms
  and browser from6000ms, without mixing both or dropping the seam sample.

- D52.4: `targets` lists eligible current sources. A system/lost fallback (its
  audio transport closed) is eligible like the D49 system/missing stand-in; its
  seam writes browser/restored instead of browser/callConnected. Run `lostSeam`
  through the same placement path as `seam`.
- D52.5: the app asks to resume a system/lost recording (its audio transport
  closed) once per loss and control connection, once more on each new hello,
  and never auto-starts a second recording of that call, matched by
  (tabId, callGeneration) across all control peers. Timing cases are the `resumeCapture` cases in
  `../multistream/lifecycle.json` (`askResume`); the auto-start exclusion is
  in `auto-start-vectors.json` (`recordings`, `expect.resume`).

Adoption is opt-in. Neither desktop should send this operation before it owns
the native `resume` handler. Extension0.2.7 is not a store upload instruction.
