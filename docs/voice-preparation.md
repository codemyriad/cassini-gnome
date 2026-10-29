# Preparing voices for older meetings

Remember needs a private voiceprint. A missing print does not establish that a
speaker talked for fewer than four seconds. The Speakers dialog distinguishes:

- Required models unavailable: explain which models to enable, with Open Settings.
- No completed extraction for this meeting: explain that its voices are not yet
  prepared; saving its name remains available.
- A successful extraction with no eligible print: show the four-second minimum.
- A usable cached print: allow Remember, including when its models are later off.

Undivided Me is never saved as a voice. A newer voice store remains read-only.
Preparation arriving while the dialog is open refreshes its controls without
replacing the entered name or its Remember choice.

After both models become ready, the Library schedules historical preparation at
low GLib priority. Each pass inspects at most eight private caches off GTK, and
admits one meeting only when the transcription/voice queue is idle. The native
child runs at nice 10 with the existing two-thread CPU configuration. New writing
down cancels that child; the historical meeting is retried after foreground work.
An extraction failure is attempted once per app launch or model-ready transition,
so an unreadable meeting cannot form a retry loop. Closing the app cancels work;
background jobs never acquire or release foreground application holds.

The job reads embedded Received words or a sidecar through the validated reader.
Words do not cross the bounded worker request pipe. Existing diarization evidence
is reused only for matching audio; otherwise the child computes overlap evidence
without writing or reassigning the transcript. Existing prints, confirmed people,
rejections, names and merges are preserved. Historical meetings are never matched
or enrolled automatically. PCM scratch stays on the app cache volume. Per-meeting
Trash exclusion, generation checks and atomic private-store transactions apply.

Verification includes a red GTK reproduction on main; truthful dialog states,
Settings navigation, late print arrival and actual Saved voices visibility;
bounded scheduling, foreground preemption and scoped cancellation; failure and
Forget-everyone cases; embedded-word reads; and the installed native worker with
the pinned public S-11 audio. The native checks preserve every meeting file and
delete their synthetic private voice caches before publishing diagnostics.
