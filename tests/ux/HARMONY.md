# Harmony contract: Cassini for GNOME and Cassini for macOS

Owner: the coordinator (Claude, acting for Silvio). Agents propose changes in their INBOX reply or in a PR, never by
diverging in code. The design canvas "Cassini recording" is the source of truth. Its frozen export is in `design/`:
- `design/png/NN-<page>-<Board>[-mac].png` is the visual.
- `design/source/<Board>.dc.html` has the exact copy and tokens. A `*-mac.dc.html` file only wraps the base board with
  `platform="mac"`. Each base board holds both platforms behind `mac` / `gnome` switches.

Silvio's resolved comments on the canvas:
- Transcription happens on this computer only. There is no remote/server option anywhere.
- Export offers exactly two formats: "Cassini file" and "Markdown". (Phase 1. Phase 2 keeps both as the two
  primary choices and adds Plain text, SRT and WebVTT under "More formats": `PARITY.md` export contract, `D99`.)

If the canvas and an older document (SPEC-CONTROL "Product behavior", ADVISOR-APP-CONTROL item 3, DIRECTION 1/2,
APP-CONTROL UX notes) disagree, **the canvas wins**.

## 1. One product, two native skins

A person who uses both apps should find the same:
- words
- states
- errors and their actions
- confirmations
- formats
- keyboard concepts
- Library folder
- export results

Only the chrome differs: window decorations, settings window type, dialog alignment, menu bar extra vs notification,
shortcut glyphs and system fonts. Everything in sections 2–9 is shared verbatim unless a row says otherwise.
Section 10 lists the only allowed platform differences, and the colour rows (10.1–10.15) that both apps share.

## 2. Vocabulary (user-facing text only; code identifiers are free)

| Concept | Say | Never say in UI |
|---|---|---|
| main view | Library (desktop); All meetings (phones, L-50) | Meetings window, Documents, Browser Meetings |
| one recording | meeting | session, recording folder, track, file (except "Cassini file") |
| start / stop | Record / Stop on all four apps (L-48; "Stop Recording" only as the Mac menu-bar item, replacing "Finish" on phones) | New Recording…, Stop and Save, Finish, Record this computer, Record Meeting |
| your mic | Your microphone / Me | Microphone input, mic track |
| remote side | The others; this computer's sound | Desktop audio, System output, Output audio, selected processes |
| browser path | Listening inside your browser; browser companion | extension, native host, control host, WebRTC |
| transcribe (verb) | write down / written down | transcribe, as a verb in sentences. Allowed: the badge "Transcribing · …" (and the provisional current ETA fallback labels), the Settings tab "Transcription", "Back to Transcription settings" |
| diarization | Tell speakers apart | diarization, speaker embedding |
| voiceprint | saved voice / voiceprint; "voice profile" only in confirmation 2 | embedding |
| storage | Music/Cassini | any path picker for recordings |

Never shown to a person: Hz, channels, PID, SHA-256, schema, fingerprint, raw GStreamer/AVFoundation/OS error text,
".opus" as a word, "Unknown Speaker", "Audio Does Not Match", "Open a Meeting".

Typography:
- Typographic apostrophes and quotes everywhere: ’ “ ”. The canvas mixes "Let's" and "Let’s"; normalise to ’.
- en-GB spelling: Recognise, recognised.

## 3. Meeting state (shared model, one badge)

The state is stored as independent fields. The badge is derived from them.

```
lifecycle   : recording | saving | saved
origin      : own | received
interruptedAtMs : null | integer      (capture ended without Stop; gap/crash/kill recovery)
transcript  : none | queued | running | done | failed | damaged
etaMinutes  : integer, only while queued/running
error       : null | { id, action }   (an entry of section 5)
```

The badge is the first matching row, top to bottom:

| Condition | Badge text (exact) | Tint |
|---|---|---|
| lifecycle = recording | `Recording 12:48` (live timer, section 6; dot glyph) | red |
| lifecycle = saving | `Saving…` (spinner) | neutral |
| transcript ∈ {queued, running} | `Transcribing · 4 min` for positive ETA (spinner; provisional variants below) | neutral |
| error ≠ null **and** error.action ≠ null | `Needs attention` | amber |
| interruptedAtMs ≠ null | `Interrupted` (alert glyph; position in the meeting) | amber |
| origin = received | `Received` (incoming glyph, transparent fill and line outline) | outline |
| otherwise | *(no badge)* | — |

L-25/L-26 additionally define `Importing…` (neutral, spinner) on an active transient import entry. It is not a new persisted lifecycle. Cross-domain priority against an existing meeting is not drawn: **PROVISIONAL, pending Alex; current value is no shared importing lifecycle or cross-domain rule**. The existing meeting precedence above is unchanged. The independent `entryVectors` array covers the Importing badge without inventing that priority.

Positive ETA uses catalog `states.badge.transcribing`, including one minute (`Transcribing · 1 min`). **PROVISIONAL, pending Alex:** zero minutes retains `Transcribing, less than a minute left`; absent/invalid ETA retains `Transcribing…`. Do not turn those into a new compact label without a decision.

Every badge is a pill (`22 px` tall, `12 px` weight-600 text, `8 px` horizontal padding, `4 px` glyph-to-label gap across all four platforms; L-54), tabular digits, with its glyph (`transcribing` spinner coloured in `accent`; L-45). Needs attention also has an alert glyph. Tone ids `accent` and `grey`, and the legacy `dot` boolean, are removed from v3 badge output; no compatibility aliases. Current model identifier `needsAttention` is retained (canvas visual name `attention`); code identifiers remain free under section2.

What the person can do (canvas "States" board):
- recording: rename, add a note, Stop.
- saving: nothing; the audio is already safe.
- transcribing: play, rename; on iOS and Android, read live draft turns under "Transcript so far" as they arrive, while on macOS and GNOME the transcript area stays empty until transcription finishes because the desktop engine does not stream lines (L-45).
- ready: everything.
- interrupted: everything, on what was captured. This is history, not an error.
- received: everything except writing it down again.
- needs attention: the one action named in its line.

Rules:
- Interrupted meetings are written down like any other. They show the Transcribing badge while that runs, then "Interrupted". The position-bearing current copy is `meeting.interruptedAt`, outside the badge.
- A received meeting whose file has no transcript may be written down once. "Except writing it down again" only
  blocks a second time.
- Clearing an error removes the badge; the meeting then shows whatever the other fields give.
- The vectors in `vectors/states/states-vectors.json` version 3 are binding. Apps run both meeting and transient-entry cases. See [Status badges](STATUS-BADGES.md) for L-25/L-26, glyphs, catalog ids and explicitly provisional current values.

## 4. Library folder

- The Library is `~/Music/Cassini` on both platforms. This is literal, not `$XDG_MUSIC_DIR`, because both browser hosts
  hard-code it today.
- "Choose folder", Move Library (confirmation 3) and the "drive not connected" banner come in a later milestone, once
  the hosts read a shared Library-location config.
- There is **no save dialog for recordings**. Record writes into the Library.
- Privacy line on first run (`L-09`, `D-938`, `D98`; shown on first run only per `L-27`, `L-30`; `PROVISIONAL` async copy review tracked in `D-1019`): Phase 2 adopts the device-specific `L-09` wording `"Written down on this {device}. The audio never leaves it."` (`privacy.onDevice.computer` on GNOME / `privacy.onDevice.mac` or `privacy.onDevice.computer` on macOS; `privacy.onDevice.phone` on Android **only in the Android phone-only state**; while phone cloud backup is on — the default — phones show the backup-qualified `privacy.onDevice.{iphone,ipad,phone}.withBackup` `"Written down on this {device}. Never uploaded, except to your encrypted cloud backup."`; iOS with backup off shows `privacy.onDevice.{iphone,ipad}.backupExcluded` `"Written down on this {device}. Cassini never uploads the audio and marks it to be left out of iCloud Backup."` and never "The audio never leaves it.", because `NSURLIsExcludedFromBackupKey` is backup guidance and does not guarantee exclusion from backups or Quick Start migration; see `PARITY.md` Wave 6 contract §§2–3), while keeping legacy `"Recordings and transcripts stay on this computer, in Music/Cassini."` (`firstRun.footer`) intact in `strings/strings.json` so existing app pins stay non-breaking.
- Ownership of the files:
  - `session.json` and the WAV files are **capture-owned**: written by the browser host or native recorder only.
  - The apps write their own data to an app-owned `meeting.json` next to them: title, notes, speakers and names
    with provenance, transcript status, error.
  - A per-app index or cache is rebuildable and never authoritative.
- Received files are copied into the Library: `Received-<uuid>/` holding the original `.opus` plus `meeting.json`
  (with `received: {originalName, originalFolderName?, sha256}`). Duplicates are detected by sha256.
- Voiceprints never go in the Library. They live in local app data (Wave 6 `D-977` GNOME, `D-978` macOS; `D25`, `D98`):
  - GNOME: `$XDG_DATA_HOME/cassini/voices`
  - Mac: `~/Library/Application Support/Cassini/voices` (local Saved voices only; never synced to iCloud or uploaded — section 12 and [`PARITY.md`](../PARITY.md) Wave 6 contract).
- The Mac agent drafts the exact layout in `ux-draft/LIBRARY.md` and `ux-draft/meeting.schema.json` (wave 1).

## 5. Errors: one line, one action, never a dialog

Errors are amber lines and info lines are accent-tinted. A line has: an icon, the line, an optional "when" subline
(debug/test only, not shown), and one action button.

Where a line appears:
- Under the Meeting headline and under the Library preview H1.
- The Library offline banner is the one exception (a banner).
- During a recording, the line also appears on the Recording page.

Lines are never shown as an AdwToast, NSAlert, AdwAlertDialog, toast or raw text.

| id | Line (exact) | Action | Platforms |
|---|---|---|---|
| mic-silent | No sound from your microphone. | Check sound settings | both |
| mic-silent (Recording page row) | No sound. Check your sound settings. | Check | both |
| others-permission | The others are not being recorded. Allow Cassini to record this computer’s sound. | Open System Settings | mac |
| disk-low | Only {n} minutes of space left. | — (D7: warn below 30 min; stop and keep at the reserve) | both |
| disk-stopped | Cassini stopped recording to keep some space free on this computer. Everything up to now is saved. | — (draft) | both |
| transcribe-failed | Could not write this meeting down. The audio is fine. | Try again | both |
| model-download-failed | Could not download {model}. Whistle keeps writing down your meetings. | Retry | both |
| others-fallback (info) | Recording the others from this computer’s sound. | Why? | both |
| transcript-damaged | This file’s transcript is damaged. The audio plays. | — | both |
| library-offline (banner) | Your Library is on a drive that is not connected. | Choose folder | both, later milestone |

Errors that exist in code today and have no drawn line yet: export failed, a file that cannot be opened or imported,
playback failed, microphone permission declined, Connect failed, device changed mid-recording.
- The coordinator adds their copy to `ux-draft/strings.json` **before** the milestone that replaces the old toasts or
  alerts.
- Until then, keep the existing behaviour. Do not invent copy.

## 6. Formats (binding vectors: `ux-draft/format-vectors.json`)

- Formatting is fixed English (en-GB patterns) with a 24 h clock. Both apps are English-only today.
- The time zone is the system time zone. The vectors pass `tz` explicitly.
- Position in a meeting: `00:14:32`. Always HH:MM:SS, zero-padded, floored to whole seconds. This replaces GNOME's
  m:ss and the Mac's formatClock.
- Live timer:
  - big timer and notification: `00:12:48`
  - badge and header pill: `12:48`, minutes unpadded, `1:02:48` from one hour on.
- Duration of a meeting: `52 min`, `1 h 03 min`. Under a minute: `under 1 min`.
- Talk time: `9 min 40 s`.
- Confirmed speech: `4 h 12 min confirmed speech`.
- Day group header: `Today` / `Yesterday` / `Friday 23 October`, with the year appended when it is not this year.
- Preview subtitle: `Today, 09:00 · 52 min`; for a received meeting, `Friday 23 October, 15:00 · 34 min · Received`.
- Search result date: `Today` / `Yesterday` / `20 October`.
- Search context subtitle: `Today, 09:00 · 52 min · at 00:14:32`.
- Row meta (L-51, L-53):
  - Desktop card line two (6px below the title): `{time}` / `{duration} · {names}` (middle dot as its own element with equal space each side; no dot after a badge). Names are in order of first speaking, unnamed speakers are `Speaker n`, and the list stops after 3 names with `+{k}`.
  - The canvas shows "2 speakers"/"3 speakers" on phones (L-52, from `1 speaker` up); desktop keeps speaker names.
  - While recording: meta `Listening inside your browser` / `Your microphone and the meeting in your browser`.
- Row quote line (**cut in UI, L-53**): Library lists on all four apps drop the transcript preview / pull-quote line under each row. Search results keep their snippet (`common.quote`).
- Skips: 10 seconds. The buttons are labelled "Back 10 seconds" and "Forward 10 seconds".
- Player: elapsed `00:03:12` and total `00:47:05`, never the remaining time. Rate shows `1×`.
- Following the transcript is implicit; there is no Follow-along switch.
- Sizes: `17 MB`, `640 MB download`, `about 24 MB`.
- Plurals: `less than a minute left` (0) / `about 1 minute left` / `about {n} minutes left`; `1 place in 1 meeting` / `{n} places in {m} meetings`;
  `1 person.` / `{n} people.`

## 7. Confirmations: only these three (Esc = secondary, Return = primary)

| # | Title | Body | Secondary | Primary |
|---|---|---|---|---|
| 1 | A recording is running | Stop and keep it, or keep recording in the background? | Keep recording | Stop and keep it |
| 2 | Delete {name}’s voice profile? | Cassini will no longer recognise {name} in new meetings. Names already in transcripts stay. | Cancel | Delete (destructive) |
| 3 | Move the Library while recording? | The meeting being recorded moves to the new folder when you press Stop. | Cancel | Move Library |

- Confirmation 1 appears when the person closes the window or quits while a recording runs. That replaces today's
  auto-stop on close and silent keep.
- "Forget everyone…" uses confirmation 2's shape. Its copy will be in strings.json.
- Deleting a meeting moves it to the Trash with no confirmation (D29). GNOME shows one undo toast for 5 s and trashes
  when it closes; macOS offers Edit › Undo Move to Trash (⌘Z) and shows no toast.

## 8. Keyboard

| Action | GNOME | macOS |
|---|---|---|
| Record / Stop (in the app) | Ctrl+R | ⌘R |
| Record / Stop (anywhere) | Ctrl+Alt+R (GlobalShortcuts portal) | ⌥⌘R |
| Find in this meeting / Search | Ctrl+F (focuses Find in this meeting when a meeting is open in the pane, L-44; sidebar search otherwise) | ⌘F (same: Find in this meeting when open, L-44; sidebar search otherwise) |
| Import audio… | Ctrl+O (L-23, L-25) | ⌘O (File › Import Audio…, L-23, L-25) |
| Export… | Ctrl+Shift+E | ⇧⌘E |
| Preferences / Settings | Ctrl+, | ⌘, |
| Play/Pause; back/forward 10 s | Space; ← / → | Space; ← / → |
| Next / previous turn (while listening, D70) | Alt+↓ / Alt+↑ | ⌥⌘↓ / ⌥⌘↑ |
| Next / previous speaker (while listening, D70) | Alt+Shift+↓ / Alt+Shift+↑ | ⌥⇧⌘↓ / ⌥⇧⌘↑ |
| Play this turn (focused transcript turn, D70) | Shift+Enter | ⇧↩ |
| Stop listening (D70) | Esc | Esc |
| Back to Library | Alt+← | ⌘[ |
| Close window / Quit | Ctrl+W / Ctrl+Q | ⌘W / ⌘Q |
| Move to Trash (Library row or open meeting) | Delete | ⌘⌫ |

- D70 listening shortcuts are disabled while editing text. Escape belongs first to a dialog or popover, then search, then an overlay speaker pane; it stops listening only after those have had it.
- Close and quit show confirmation 1 while recording.
- On both platforms, Settings › Recording shows the global shortcut with one "Change…" action. GNOME opens the portal's
  ConfigureShortcuts; Mac opens an in-app recorder.
- In-meeting find (`Find in this meeting`, `Ctrl+F` / `⌘F`) is restored on both desktop apps inside the open meeting pane (L-44), matching the phones; clicking a Library search result opens the meeting on the matching line.
- Removed on both platforms: New Window, New Recording…, Browser Meetings shortcut, Follow along, Jump to current,
  Inspector, speed shortcuts, 5 s and 15 s skips, Ctrl+E.
- The GNOME shortcuts dialog and the Mac menus list exactly this table.

## 9. Export (boards 21/22)

- Title: `Export “{title}”`. Subtitle: "Export is the only way a meeting leaves this computer."
- "What to export":
  - **Cassini file** (default): "Audio with the transcript inside. Plays in any audio player.", with the size
    `about {n} MB`.
  - **Markdown**: "The transcript as a text file, with speakers and times."
- "Speaker names": "The file carries the names in this meeting, including {name}, who was recognised automatically.
  Voiceprints are never exported." With no recognised names: "The file carries the names in this meeting. Voiceprints are
  never exported." With two or more: "…including {names}, who were recognised automatically. …", with the names semibold
  and joined as "Dario and Ana" or "Dario, Ana and Lea" (no Oxford comma; only the names are semibold).
- "Save to **Documents**", with "Choose…" next to it. Buttons: Cancel, Export.
- Entry points: the header "Export…" button, the menu item "Export…" and the shortcut.
- Removed on both platforms in Phase 1:
  - Mac ShareLink, Plain Text, WebVTT, SRT
  - GNOME TXT, WebVTT, Ctrl+E
- Phase 2 (`L-11`, `D-998`, `D99`): one export set on all four apps. Plain text, Subtitles (SRT) and Subtitles (WebVTT)
  return under a "More formats" disclosure below Markdown, for transcribed meetings only; an imported meeting without a
  Cassini file offers "Original audio" instead. Rules and catalog ids: `PARITY.md`, Phase 2 parity contract §2.
- The Markdown output is byte-identical between the apps. The golden file is `ux-draft/export/budget-review.md`
  (coordinator, wave 2).

## 10. Allowed platform differences (and only these)

| Area | GNOME | macOS |
|---|---|---|
| Main window | AdwNavigationSplitView. Sidebar header: `☰` menu at start (L-23: `Import audio…`, `Preferences`, `Keyboard shortcuts`, `About`), `Import audio…` icon button + `Record` (or live pill) at end (L-25). Sidebar top: always-visible search field (L-21). Right pane: selecting a meeting opens it in one step (no intermediate preview or "Read the transcript" button); top bar has no "Library" title, only flat meeting actions (`Export…`, `Move to Trash`, `…`) beside window close (L-22, L-23, L-44). | NavigationSplitView. Traffic lights; `Import Audio…` icon button + `Record` (or live pill) in the sidebar header (L-25); always-visible search field at the top of the sidebar (L-21, no toolbar search field). Menu bar `File › Import Audio…` (`⌘O`, was `Open…`, L-23). Right pane: opens in one step; detail toolbar has no "Library" title, grouping `Export` and `Move to Recently Deleted` in one capsule (L-22, L-44). |
| Toolbar Record button | red fill, white dot, white "Record" (10.2) | the same as GNOME (L-20, 10.2) |
| First-run Record button | big red pill (`record` / `on-record`, 10.2) at the start of the hero waveform on both; first press opens `RecordFirst` (L-20, L-27) | the same as GNOME (L-20, L-27, 10.2) |
| First run layout | Single-pane Phase 2 `L-27` hero (`D-956`, `D-972`): header keeps primary menu (`☰`) and window close, with no header `Record` button and only a hidden accessible `"Library"` heading (`library.title`). Left-aligned hero block headed by the Saturn mark (`COMPONENTS.md` §§8–9): `64 px` bold platform-font `"Every word written down."` (`firstRun.hero.title`) over dimmed `"None of it uploaded."` (`firstRun.hero.subtitle`) with **no** cloud-backup qualifier, always one sentence per line; square `4 px` teal bar waveform fading at both ends with the big red `Record` pill at its start; how-to line (`firstRun.body`); and provisional `L-09` privacy line (`privacy.onDevice.computer`, `D-938`, async review in `D-1019`; `firstRun.footer` kept in catalog) on first run only (`L-30`). *(Phones pair `"None of it uploaded."` with `firstRun.hero.phoneBackupQualifier` `"(except for encrypted cloud backup)"` while OS cloud backup is enabled per `D-937` and [`PARITY.md`](../PARITY.md).)* | Single-pane Phase 2 `L-27` hero (`D-956`, `D-973`): window header keeps traffic lights (menus in the macOS menu bar), with no toolbar `Record` button and only a hidden accessible `"Library"` heading (`library.title`). Same left-aligned hero block headed by the Saturn mark: `64 px` bold platform-font `"Every word written down."` (`firstRun.hero.title`) over dimmed `"None of it uploaded."` (`firstRun.hero.subtitle`) with **no** cloud-backup qualifier, always one sentence per line; square `4 px` teal bar waveform fading at both ends with the big red `Record` pill at its start; how-to line (`firstRun.body`); and provisional `L-09` privacy line (`privacy.onDevice.mac` or `privacy.onDevice.computer`, `D-938`, async review in `D-1019`; `firstRun.footer` kept in catalog) on first run only (`L-30`). *(Phones pair `"None of it uploaded."` with `firstRun.hero.phoneBackupQualifier` `"(except for encrypted cloud backup)"` while OS cloud backup is enabled per `D-937` and [`PARITY.md`](../PARITY.md).)* |
| Search field | always visible at top of sidebar (`field` / `field-line`, 10.14), placeholder "Search every meeting" (L-21) | always visible at top of sidebar (`field` / `field-line`, 10.14), placeholder "Search" or "Search every meeting", accessibility label "Search every meeting" (L-21) |
| Selected row | neutral lifted card (`surface` in light, slightly raised in dark) with firm `ink` border and soft shadow, `ink` text, no accent fill (L-19, 10.9) | the same neutral lifted card as GNOME (L-19, 10.9) |
| Meeting & Recording pane | Meeting opens in right pane: heading with rename, date + length, inline speakers (colour dot + name, soft underline when unnamed, L-43), `Find in this meeting` (`Ctrl+F`), floating player; scrolled moves title + date into compact header (L-44). Transcribing shows accent spinner + status line + thin bar + `Stop` + chevron popover (L-45). Recording pane shows heading + `Today, HH:MM`, compact 30px timer + 32px red `Stop` pill, single sources card + `Change what is recorded` (L-47). | Same one-step pane, inline speakers, `Find in this meeting` (`⌘F`), floating player, scrolled compact header (L-44), Transcribing status line + popover with battery at finish (L-45), and Recording pane with 30px red `Stop` pill (L-47). |
| Name a speaker dialog | Centred floating dialog (`14px` corners) over the meeting pane with full-window scrim; single column (`Speaker n` heading + talk time, quote with left bar, focused `Name` field, neutral saved-voice duplicate notice `{name} is already a saved voice.` with inline small buttons, `Saved voices, closest first` heading, neutral `ink`-at-10% `Likely` tag with no row highlight, `Remember this voice` card with switch, footer `Merge into…` left + `Cancel` / `Save name` right; L-46). | Single-column floating dialog (`12px` corners) over the meeting pane with full-window scrim, small macOS switch, `Saved voices` heading above the group and `Closest first` footer below it, neutral `ink`-at-10% `Likely` tag with no row highlight, and `{name} is already a saved voice.` duplicate notice (L-46). |
| Dialogs | AdwDialog / AdwAlertDialog; centred title and body; equal-width buttons | sheet under the toolbar; leading title and body; trailing buttons at natural width |
| Settings | "Preferences": AdwPreferencesDialog with 4 pages (Recording, Transcription, Library, Browser companion). Saved voices pushed as a subpage (Wave 6 `D-977`: rename, delete/forget, sample count/status, transcript clip + guided test-phrase enrollment; local store only). | "Settings": Settings scene TabView with the same 4 tabs. Saved voices pushed in a NavigationStack (Wave 6 `D-978`, `D-1000`: local Saved voices in `~/Library/Application Support/Cassini/voices`, zero iCloud sync per section 12). |
| Recording with the app hidden | GNotification updated each second: "Recording, 00:12:48" / "{title}. Your microphone and the meeting in your browser." with Stop (destructive) and Open Cassini. A status icon where the desktop shows them. | MenuBarExtra showing a dot and "12:48". Menu: "Recording, 00:12:48", title, two level rows, "Stop Recording ⌥⌘R", "Add a Note…", "Open Cassini", "Your Mac will not sleep while Cassini records." |
| RecordFirst captions | "Your desktop may ask you to allow this next." / "Nothing to allow on this computer." | "macOS asks you to allow this next." / "macOS asks once more, for Screen & System Audio Recording." |
| others-permission line | absent | present |
| Shortcut glyphs | Ctrl+R, Ctrl+Alt+R | ⌘R, ⌥⌘R |
| Body / small text size | 15 / 13 px | 13 / 11.5 px |
| Fonts | system UI font | system UI font |
| Accent, amber on tint | `accent`, `warn-ink` on `warn-tint` (`brand/tokens.json`, L-06, L-17, L-39) | the same Cassini `accent` and `warn-ink` on `warn-tint` from `brand/tokens.json`; ignores macOS system accent colour (L-06, L-17, L-39) |
| Sidebar background | Adwaita sidebar colour mapped to `sidebar` (L-42, 10.14) | native macOS sidebar material (`sidebar-mac` reference in `brand/tokens.json`, L-42, 10.14) |
| Radius: row / card / dialog / window | 10 / 12 / 14 / 14 | 7 / 10 / 12 / 10 |

- Colours come from `brand/tokens.json` (10.1), not from the system accent, destructive or warning colours (L-06,
  L-16).
- The no-sleep tooltip on the Recording page pill is "Your computer will not sleep while Cassini records." on both
  platforms (as drawn). The Mac menu-bar line says "Your Mac…" (as drawn).

### Colours: the same on both apps (rows 10.1–10.15)

Alex decided the colours on 12 November (L-07, L-14, L-17, L-24, L-36 to L-42, C18). They live in one file,
`brand/tokens.json`. These rows say which role each part of the window reads; they name roles, never values, so a
value change is a pin bump and never a contract change. Role names are the file's names: CSS `--cx-<name>`, Swift
camelCase with the digits kept (`ink3`, `errorFill`). "White text" means `on-record`, the white the file pairs with
the Record, error and success fills.

Status: **decided** means build it as written. **Provisional, pending Alex** means build it with the role and value
the file has today; Alex may still change it, and the app changes only through a pin bump or the one helper the row
names. Since Alex's reply of 12 November no row is provisional. Each row is checked by the app's token test (values, both modes) and its light and dark screenshots (use).

| # | Rule | Status |
|---|---|---|
| 10.1 | **One source.** Every colour the app draws MUST come from `brand/tokens.json`, vendored at one pinned cassini-ux commit with its SHA-256 (README, "How apps vendor"). The app turns it into its own token file(s), one per language it draws with (for example CSS and Python on GNOME, one Swift file on the Mac); no other app file holds a colour value. The app reads roles whose `status` is `decided` or `placeholder`, never `proposed`; a `pending` role has no value to read. CI MUST fail when: the vendored copy does not match the pinned SHA-256; any `decided` or `placeholder` role in the app's token file differs from the vendored copy in light or dark (drift); or a colour literal appears outside the generated token file(s) (hex, `rgb()`/`rgba()`/`hsl()`, or a fixed platform colour such as `NSColor.systemRed`, `Color.red` or a libadwaita palette colour like `@red_3`). Vendored brand images (the Saturn mark) and system chrome the app does not draw (window controls, traffic lights) are outside this row. In cassini-ux, `python3 -I brand/validate.py` MUST pass on every commit an app pins. | decided |
| 10.2 | **Record button.** The Record button (toolbar and first run) MUST be filled with `record`, with its dot and "Record" in white (`on-record`), in light and dark, on both apps (L-07, L-20). The Mac grey fill with a red dot goes. | decided |
| 10.3 | **Record red as text.** The live dot and red text that belong to a recording (the live pill, the `Recording 12:48` badge) MUST be `record-ink`, which is the Record red in light and a lighter red in dark. `record` is a fill only and MUST NOT be used as text in dark. A red wash behind them MUST be `record-tint` (L-17: accepted as drawn). | decided |
| 10.4 | **Error red as text.** Error text and error icons drawn in red MUST be `error` (light: the Record red; dark: the same lighter red as `record-ink`). A destructive button drawn as red text with no fill (for example an AdwAlertDialog destructive response) also uses `error`. White text MUST NOT sit on `error`. Section 5 lines stay amber (`warn-ink` on `warn-tint`); this row does not change them. GNOME maps `--error-color` and `--destructive-color` to `--cx-error`. | decided |
| 10.5 | **Error wash.** A red wash behind error content MUST be `error-tint`, which carries the Record wash's value, so each mode has one red family (L-40). Error text on it is `error`. | decided |
| 10.6 | **Destructive fill.** A destructive button drawn with a fill MUST be filled with `error-fill` with white text (`on-record`) in light and dark (L-37). It MUST NOT use `error`, `record` or the system red as its fill. GNOME maps `--destructive-bg-color` to `--cx-error-fill` and `--destructive-fg-color` to white; the Mac does not use `systemRed`. | decided |
| 10.7 | **Success fill.** A filled success button MUST be filled with `success-fill` with white text (`on-record`) in light and dark (L-38). White text MUST NOT sit on `success`, which is a text colour; success text uses `success`, and a success wash uses `success-tint`. GNOME maps `--success-bg-color` to `--cx-success-fill` and `--success-fg-color` to white. | decided |
| 10.8 | **Placeholder grey.** Placeholder text in a text field (for example "Search every meeting") MUST be `ink-3` at full opacity, with no extra alpha on top, on every ground including the sidebar (`sidebar`) and text fields (`field`) (L-36, L-42). GNOME styles `text > placeholder` with `--cx-ink-3` and `opacity: 1`. | decided |
| 10.9 | **Resting card.** Where a meeting is drawn as a card (L-19), the card at rest MUST be filled with `card-rest` from the file's `derived` block: in dark that is `surface` (C18); in light it is halfway between `surface` and `ground`. The selected card is not covered here. | decided |
| 10.10 | **Speaker colours.** A named speaker's name (turn label, chip, avatar initial) MUST be drawn in one of `speaker-1` to `speaker-6`, in order of first speaking: blue, magenta, cyan, orange, violet, slate. The seventh starts again at `speaker-1`. Red, green and yellow stay with the app (Record and errors, success, warning), so no speaker reads as status. A speaker not named yet MUST be `speaker-0` (stone), whatever its place in the order. The words a speaker said MUST be `ink`: names in colour, words in ink. No speaker reads `accent`, `record`, `error` or a platform colour. The old desktop speaker palettes go (L-14; no migration, D51). | decided |
| 10.11 | **Which slot.** The colour number is fixed when a speaker first speaks: the writer stores it as `colourIndex` (from 0, in order of first speaking) and it never shifts. A speaker shows stone until named; naming reveals the slot it first spoke in, never the next free one (L-41). The app MUST pick the slot in one helper: `speaker-((colourIndex mod 6) + 1)`, and `speaker-0` for `provenance: "unnamed"` whatever its index. Avatars and chips use the same n (10.12). | decided |
| 10.12 | **Avatars and chips.** A speaker's avatar and name chip MUST be the `avatar-n` wash with the initial or name in `speaker-n`, with the same n as 10.10 and 10.11; a speaker not named yet uses `avatar-0` with `speaker-0`. Saved-voice avatars use the same pairs, replacing the four old desktop tints. A saved voice takes its colour once, when it is saved: the k-th voice saved (from 0) gets n = (k mod 6) + 1, and keeps it however the list is sorted (L-41); the colour does not follow list position. The washes are final (L-14, L-17). | decided |
| 10.13 | **Derived roles.** The derived roles (`sunken`, `ink-2`, `ink-3`, the tints, the speaker and avatar colours) were accepted as drawn (L-17); the file carries no `check` marks, and they are read like any other role. A later value change is a pin bump only. | decided |
| 10.14 | **Sidebar.** GNOME MUST fill the sidebar with `sidebar` (mapped to `--sidebar-bg-color`); the Mac MUST keep the native sidebar material, which the base is tuned to (`sidebar-mac` is its reference value for mocks and tests, not a fill). Text fields on a sidebar MUST be `field` with a `field-line` border on both apps (L-42). | decided |
| 10.15 | **Neutral pills and secondary buttons.** Neutral badges (Transcribing, Saving, speaker and bookmark counts) and secondary buttons MUST fill with `ink` at 10% over whatever is behind them, not `neutral-tint`, which vanishes on dark cards and dialogs (L-46, L-54). | decided |

## 11. Not drawn yet: do not invent, ask

The coordinator drafts copy for the items below in `ux-draft/strings.json`, and Silvio approves it. Until a string
exists, leave the surface out of the UI, or keep the existing behaviour unchanged.

Not drawn (or deferred to Phase 2):
- Settings tabs Recording, Library and Browser companion (Phase 2).
- Summary generation and schema (`L-44` draws Summary/Transcript tabs when a summary exists, but `meeting.schema.json` has no summary field yet; Phase 1 builds the no-summary layout with no tabs).
- Phase 2 desktop menu additions (`L-23`, approved to build as capacity allows under `D-1000` / `D-978`, `D98`): in-app Trash/Recently Deleted list, Help, Mac `Saved Voices…` (`savedVoices.menuItem`) menu item.
- Notes in the Meeting view.
- Capture-path variants of "Listening inside your browser".
- Two-speaker labels when "Tell speakers apart" is off.
- Companion-setup states other than the drawn one.
- Empty search results.
- Extension popup copy.
- Info.plist and portal permission reasons.

## 12. Models and the network (DECISIONS-UX D4, D98)

- Models are downloaded only from https://dist.gocassini.com, using its manifest, by `(id, source_sha256)`.
  Verification and install rules are in D4 and are identical on both apps.
- Downloads happen only when the person asks. Each file is checked against both sha256 values before it is used.
- The only other network use is checking for updates, which sends no meeting data.
- **No cloud backup or iCloud sync on desktop (`L-28`, `D-957`, `D-978`, `D-1012`, `D98`):** Neither GNOME nor macOS uploads meetings, transcripts, or saved voiceprints to iCloud or any cloud backup service. macOS builds **local** Saved voices (`D-978`) in `~/Library/Application Support/Cassini/voices` without iCloud network use, so the desktop first-run hero keeps `"None of it uploaded."` (`firstRun.hero.subtitle`) unqualified (§10). Encrypted OS cloud backup (`L-28`, `"Include saved voices in backups"` ON by default, dismissible backup card: Android `"Keep them on this phone only"` turns off both cloud backup and automatic device-to-device transfer; iOS `"Leave them out of iCloud Backup"` only marks data as excluded from iCloud Backup, with no "phone only" claim) and the `"(except for encrypted cloud backup)"` hero qualifier (`D-937`) apply to the phone apps (`iOS` and `Android`) only — see [`PARITY.md`](../PARITY.md).
- Whistle is bundled in both apps with identical pinned weights and engine (D5). Every process that loads the
  engine sets `NEEDLE_TELEMETRY=0` and `DO_NOT_TRACK=1` (and `ORT_DISABLE_TELEMETRY=1` for ONNX Runtime). The same
  audio must produce identical words in both apps.
- Size copy is computed from the manifest; it is never typed by hand.

