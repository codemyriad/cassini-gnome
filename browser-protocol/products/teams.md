# Microsoft Teams web: runnable manual procedure

Required: 2 participants including the recorder, independent test profiles/accounts;
440 Hz local / 880 Hz remote. Complete setup.md first. Status: no live version pass recorded yet.

1. Join the dedicated test call. Confirm the far-end tone is audible before recording. Note
   Chrome/product versions and each participant's input frequency. Approve test guests if needed.
2. Open Cassini popup and click **Record separate tracks**. Note the shown source count and
   complete Recording-UUID path. If still waiting after joining, record that as a gap; never
   infer success from a red badge alone. A single mixed remote receiver is allowed. Record the actual source count/kinds; do not label the mix as one verified person.
3. Record at least 10 seconds of steady signal. Mute the local participant in the product for
   10 seconds, then unmute for 10 seconds; log elapsed times. Expect local silence and continued
   far-end audio. Select a settled mute interval with summarize.py --start/--duration.
4. Select a second microphone in the product's device menu, then restore the first. Record
   timing, track changes and continuity. If only one fake/physical input exists, mark blocked;
   restarting a browser is not a device-switch pass.
5. Mute/unmute the far end  for 10 seconds each. Observe source
   content/count/labels. Compare known tone powers; mixed remote content is not a bleed failure.
6. Click **Stop and save**, wait for saved status, retain session.json/WAVs and run
   `python3 conformance/summarize.py /absolute/Recording-UUID --tones 440,880`.
   Require desired tone power >0.001 and unwanted tone <1% for separated tone sources.
7. Run a new **Record the whole tab** recording for at least 10 seconds. Confirm playback remains
   audible and one stereo `tab` source contains the rendered far-end mix. Do not assume local
   microphone inclusion. Click **Stop and save** and collect that folder/summary separately.
8. In one final short recording, leave or navigate the meeting tab while audio is active.
   Require a visible interruption and playable partial WAVs. Complete TEMPLATE.md, attach
   session.json and summarize output for each mode, and record every blocked or failed step.

Only sanitized handwritten observations go into results/. CI fixture outputs are Actions
artifacts and cannot be substituted for the live product/version result.
