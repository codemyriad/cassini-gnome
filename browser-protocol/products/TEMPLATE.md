# Manual product compatibility result

- Product, exposed build/version or observation URL/date (remove invite tokens):
- Tester/date; OS; exact Chrome version:
- App commit; browser-protocol/PINNED_COMMIT:
- Local profile; independent participant count and profile/machine setup:
- Signal source per participant (frequency/fake WAV or controlled speech); headphones:
- Capture mode (exact popup button); observed kinds/count/labels:
- Recording folder path (sanitize username for committed results):
- session.json attachment/path:
- summarize.py command and complete output attachment/path:

| Action / elapsed session time | Expected signal or lifecycle | Observed | Pass/fail/blocked |
| --- | --- | --- | --- |
| Join, before Record | Call audio audible; no recording folder created by capture | | |
| Record separate tracks | Incoming/outgoing sources, or explicit waiting if inaccessible | | |
| Local mute for 10 s, then unmute | Local settled RMS <0.005; remote tone continues | | |
| Change microphone, then restore | New/resumed source stays on timeline; blocked if no second device | | |
| Far-end mute/speaker change for 10 s | Receiver content changes without inventing participant identity | | |
| Stop and save | Finalized readable WAVs and completed/interrupted explanation | | |
| Separate tab fallback run | One stereo tab mix; playback preserved, mic not assumed | | |
| Leave/navigation while recording | Preserved partial WAVs and visible interruption | | |
| Replay/measure settled intervals | Desired power >0.001; unwanted <1% unless expected mixed content | | |

Known gaps, unavailable account/device steps and follow-up:

Result type: live manual run (not a controlled topology fixture). No success claim for blank rows.
