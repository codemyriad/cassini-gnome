# Library round-trip vectors — D112 (L-86, L-87)

Three deterministic, CC0-1.0 round-trip vectors (`meeting.json` → `.opus` → `meeting.json`) testing the shared meeting-file fields and legacy-date rules introduced in `D112`:

1. **`background-voice`** (`L-86`): `speakers[].background: true`
   - `background-voice.meeting.json`: four speakers, two marked `background: true` (one named `"Marta"`, one unnamed), proving `name` is preserved when `background: true` is set.
   - `background-voice.opus`: Library v2 Cassini `.opus` file with `x["codemyriad.io"].meeting.speakers` carrying `background: true` verbatim, manifest `speakers[]` keeping all four original speaker ids unchanged (`CASSINI_SPEAKER_COUNT=4`), and the transcript body unchanged.
   - `background-voice.expected.json`: exact rebuilt `meeting.json` plus expected visible speakers, row meta names, and folded consecutive background turn run (`count: 2`).

2. **`user-started-at`** (`L-87`): `startedAt` + `startedAtSource: "user"`
   - `user-started-at.meeting.json`: a received recording originally imported with `namePrefixSource: "import-time"` whose recording start was set by the person (`startedAt: "2026-11-23T14:36:00+01:00"`, `startedAtSource: "user"`).
   - `user-started-at.opus`: Library v2 Cassini `.opus` file carrying `startedAt` and `startedAtSource` in `x["codemyriad.io"].meeting` (never in `passthrough`), projecting `DATE` and manifest `meeting.startedAt` to `"2026-11-23T14:36:00+01:00"` and `meeting.recordedAtLocal` to `"2026-11-23T14:36:00"` (no offset, no zone, no fraction), while keeping `namePrefix`, `namePrefixSource` and `meeting.createdAtUtc` unchanged (setting a date never renames the file).
   - `user-started-at.expected.json`: exact rebuilt `meeting.json` and expected projected tags/manifest fields.

3. **`legacy-import-time-date`** (`L-87`, supersedes S-12 §11 step-3a #8): legacy synthesized `DATE` exclusion
   - `legacy-import-time-date.meeting.json`: an unknown-start received file tagged `namePrefixSource: "import-time"` with no `startedAt` / `startedAtSource`.
   - `legacy-import-time-date.opus`: simulates a pre-D112 file where publish time (`2026-11-24T09:15:00+01:00`) was written into the Vorbis `DATE` tag while `namePrefixSource` is `"import-time"`.
   - `legacy-import-time-date.expected.json`: asserts readers exclude the legacy synthesized `DATE`, resolving `startMs: null` (`inDateUnknownGroup: true`).

## Checks

```bash
python3 -I vectors/library-vectors/round-trip/verify.py
```

To regenerate (deterministic, stdlib only):

```bash
python3 -I vectors/library-vectors/round-trip/build.py
```
