# Test participants, audio and evidence

Use a local recorder participant and an independent far-end browser/profile or machine.
A second tab sharing the same microphone is not an independent far end. Use headphones; the
recorder microphone must not acoustically hear the far-end tone. Do not invite real meetings.

1. Install the released extension/host, reload the test meeting, and record the app and
   browser-protocol/PINNED_COMMIT revisions. Open the popup before joining; no recording yet.
2. Generate fake microphone WAVs:
   `python3 conformance/make-test-tone.py /tmp/cassini-local-440.wav --frequency 440`
   and `python3 conformance/make-test-tone.py /tmp/cassini-remote-880.wav --frequency 880`.
   Meet additionally uses 220 Hz locally and 440/660/880 Hz on its three remote participants.
3. On macOS a dedicated far-end Chrome can be launched with:
   `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --user-data-dir="$HOME/cassini-far-end-test" --use-fake-device-for-media-stream --use-file-for-fake-audio-capture=/tmp/cassini-remote-880.wav`.
   Sign that profile into the required test account and select its fake microphone. Close only
   that test instance before changing launch flags; flags do not reconfigure a running instance.
   The existing local profile may be ~/cassini-ext-test. Use its fake input similarly, or a
   real microphone with a controlled spoken phrase and record that weaker signal check.
4. Start the product-specific procedure. Record elapsed session time of mute/unmute, device
   switch, far-end change, leave and Stop. Always use **Stop and save**; wait for saved status.
5. Retain the popup's complete recording folder path, session.json, per-track WAVs and summary.
   Example: `python3 conformance/summarize.py /path/Recording-UUID --tones 440,880`.
   For a settled 6-second interval inside a 10-second mute:
   `python3 conformance/summarize.py /path/Recording-UUID --start MUTE_TIME_PLUS_2 --duration 6 --tones 440,880`.
   --start is session-relative seconds, not wall-clock time; unknown starts cannot be aligned.

Measurable checks for fake-tone runs: desired tone power >0.001; unwanted tone power less
than 1% of the desired tone (-20 dB power). For a muted local source, settled RMS <0.005;
far-end tone should remain RMS >0.01. summarize's powers describe the first audible half-second
of the chosen interval. Inspect the event interval rather than averaging the whole call.
A mixed remote stream may correctly contain several far-end tones; that is not local bleed.
With real speech, describe what was audible and do not claim the spectral threshold was met.
Keep credentials, invite tokens, real participant identities and real conversation audio out of Git.
