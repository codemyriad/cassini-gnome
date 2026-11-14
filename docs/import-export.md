# Received import and single-meeting export (W4-2, L-11)

Open, Open with and file drops copy Ogg Opus into an atomically published
Received-UUID folder, select its Library row and leave the original untouched.
The full-file SHA256 deduplicates identical bytes. A stored hash is checked
against the actual existing copy before deduplication, so a missing or changed
copy does not swallow a good incoming file. Source reads require a regular file;
copying is bounded, staged under the Library and published after metadata. Folder
and session.json opens keep their existing read-only outside-Library behavior.

Received context uses the shared folder-card copy when the original folder name
is known. The received-only hint is shown when no own meetings are present.
There is no new duplicate feedback or missing-folder variant. File/import errors
keep the Library open with the diagnostic; friendly error copy can be published later.

Export offers Cassini file (default) and Markdown, through the header, menu
and Ctrl+Shift+E (the old Ctrl+E shortcut stays removed). Phase 2 (`L-11`, `D99` §2,
PARITY §2) adds a collapsed "More formats" disclosure below Markdown with Plain text
(`.txt`), Subtitles (SRT) and Subtitles (WebVTT), for transcribed meetings only
(`export_model.available_formats`). A meeting whose transcript is none, queued,
running, failed or damaged is audio-only: Markdown and More formats are hidden (not
greyed out), `export.audioOnly.note` is shown, and only the audio choice remains: a
Cassini file with an empty word-timed transcript (`D12`), or "Original audio" for an
imported plain-audio file (`Meeting.plain_audio`), which copies the stored imported
bytes unchanged with their own extension. A transcript that stops being complete
while the dialog is open hides the text rows and falls back to the audio choice;
`export_meeting` also refuses text formats without a completed transcript.

Text formats share the Markdown projection, so names are exactly as shown (unnamed
`Speaker {n}`) and voiceprints and notes never appear (`D25`). Plain text is one
block per reading row, `{name} · {hh:mm:ss}` then the row's words (inline replies
as in the viewer). SRT and WebVTT cut every row and every inline reply into cues of
at most 7 s and two 42-character lines (a 1.5 s pause also starts a cue; a cue lasts
at least 1 s unless the same speaker's next cue starts first). SRT prefixes every
cue with `{name}: `; WebVTT writes `WEBVTT` and the same cues as `<v {name}>` spans,
escaping `&`, `<` and `>` in names and words. Control characters in names and words
become spaces so nothing can forge a block or a cue. Every format is staged next to
the destination, fsynced, published without replacing a file, and the folder fsynced.
The shared dialog discloses every automatically recognised name, with only the
names semibold, no Oxford comma and no truncation; long content scrolls. Choose
uses the native/portal folder chooser. Documents is only its initial suggestion;
until a folder has been chosen, Export opens that chooser before starting work.
The returned folder URI is remembered in GSettings for later exports, and Choose
updates it without opening a second chooser on Export. Cancel cancels an
outstanding chooser or background job. Output files publish without replacing an existing
file, use safe bounded names and append a number on collision. Failed exports
retain the old error toast until new error copy is drawn.

Markdown is UTF-8/LF with one trailing newline, exact S-5 escaping, consecutive
speaker turns, first-speaking speaker order and time-sorted notes. The two
published byte goldens are unchanged. An unavailable date is omitted, as no date
is known; a known duration is retained. UI callers supply the system time zone.

Cassini export writes all v1 descriptor tags and independently readable payloads.
PCM sources retain their known offsets, stereo is averaged to mono and tracks
are summed without normalisation. A bounded five-ms lookahead, 50-ms release
limiter caps peaks at 0.95 while retaining quiet levels and duration. GStreamer
resamples into mono 48 kHz and encodes VBR, complexity 10, application audio with
D8's band and bitrate. Unknown non-empty offsets are never guessed. D8's source
bandwidth includes empty tracks. The published file contains 15 choice vectors
in total, including its three desktop PCM cases; all are copied verbatim and run.
The board's actual 8 kHz Budget fixture therefore estimates about 6 MB, rather
than inventing the drawing's 48 kHz/about-24-MB source.

Existing single-stream Opus is never decoded or re-encoded for export (D13).
Only tags and framing change; stereo, packets and exact-opus-audio-v1 integrity
remain unchanged. Audio-encode policy is carried only when bound to the same
verified audio. Header continuation and a header page shared with audio are
handled without changing packet bytes. Work is streamed in bounded pages; no
expanded intermediate recording is written to /tmp or anywhere else.

D12: a non-complete transcript exports one empty words body and no speech-to-text
provenance/readable transcript. Import/Open classifies all-empty word bodies,
including null items, as none and keeps Write it down available. An empty default
with a non-empty alternative does not count as all-empty. D13 notes use only
x["gocassini.com"].notes in their stored order; other private domains and unknown
owned-domain members survive a rewrite. Saved voices are not read or exported.

Tests pin the shared Markdown/choice files, the format's CC0 pure reader and
manifest schema at 867f68c. They validate actual encoded audio, stereo averaging,
offsets, unchanged source bytes, collisions/cancellation and empty/notes/private
round trips. Private GTK checks exercise Open/drop, silent dedupe selection,
folder context, playback, both exports and 0/1/several recognised-name variants.
The offline container proof re-exports a real two-hour stereo Opus inside the
existing read-only/networkless harness with 64 MiB /tmp, unchanged audio digest
and channels, no temporary WAV and bounded memory (export-long-linux.json).
Screenshots are received.png and export.png in the light/dark ux-boards artifact.
No host desktop, audio device or browser profile is used by those checks.

Export rechecks streaming read errors after a blocked sample pull before accepting
EOS, so a failed WAV read cannot publish a truncated or missing-track success.
Received audio headers are read through the same no-follow regular-file handle as
the audio digest, avoiding symlink/FIFO header opens. Portable-reader cancellation
is converted to the export dialog's cancellation type only when its cancel event
is set; unrelated IO interruptions remain errors. Empty embedded speaker IDs are
unreadable, and a successfully written empty sidecar stays done even if the
original portable file contained an empty transcript.
Received imports normalize the copied filename to the shared lowercase `.opus`
folder convention (including `.ogg`, `.OPUS` and backslashes in source names).
The audio bytes stay unchanged. Source paths are normalized before taking the
folder hint; an unrepresentable folder hint is omitted. Metadata schema and
encoded-size validation happen before publication, and a failed first scan
removes the newly published folder. Failed imports leave the Library visible
with the failure diagnostic instead of reopening the original in the legacy
app. The isolated Open/drop probe covers both filename variants, deduplication
and a header-readable stream missing its ending.

The scanner carries a received file's byte size into its immutable Meeting model;
Export does not stat that file on the GTK thread. An open idle Export dialog
tracks fresh Library metadata, title and recognised-name disclosure. Pressing
Export takes the current Meeting-page/Library snapshot; an export already in
progress retains its chosen immutable input. The isolated GUI proof rejects
Python stat calls during dialog construction, changes real metadata while the
dialog is open, and checks the updated title and note in the resulting file.

D19 recovery exports stamp `createdAtUtc` at export time. A known meeting start
is retained separately as the optional local wall-clock `recordedAtLocal`; an
unknown start remains absent. Export mixes unknown source offsets at zero using
copies of the track descriptors and records the assumption in
`x["gocassini.com"].recovery.offsetsAssumedZero`. Capture files and the Library's
unknown offsets remain unchanged. Import/re-export carries that private marker.

D35.1 completes the no-private-home path alongside the Library persistence fix
(#104). Export rejects an unbacked Flatpak home destination before creating any
folder or staging output; no broad filesystem permission is added. Its successful
publication shows the catalog’s Exported… toast with Show, which opens the
published file’s containing folder. Existing collision, verification and atomic
publication rules are unchanged. Native GTK tests drive actual FileDialog folder
selection, remembered destination, picker dismissal/cancellation, both formats,
feedback and Show dispatch. They fail on any Gtk-WARNING/CRITICAL.

The installed Flatpak persistence test covers localized, absent, disabled and
revoked Music, two launches each. It verifies recording bytes and both exported
formats survive, and rejects a writable private-home export. Its narrow per-run
folder grant models the selected folder; it does not claim to test document-portal
FUSE mounting. The roy host has no /dev/fuse. The native chooser proof and real
sandbox/restart proof are separate; end-to-end document-portal mounting remains
a desktop QA check. Main fails the same sandbox test by accepting the unsafe
private-home export.

## Batch and ZIP archive import (D96)

Open (`open_multiple`), OS file activation and drag-and-drop accept several
files at once and `.zip` archives using `import.filter.cassiniAndZip`. Multi-file
and ZIP imports run on a dedicated single-worker `ThreadPoolExecutor` so the
Library monitor continues scanning without contention. Queued imports present
their `Adw.Dialog` (`import.progress` + `common.cancel`) only when started;
closing the window cancels active and queued jobs.

Each archive is checked from its directory before any member is imported, and
the byte and ratio bounds are enforced again on the bytes actually read:

- At most 2,000 entries per archive (`MAX_ENTRIES = 2_000`) and 2 MiB central
  directory metadata before `ZipFile` allocation (`import.zip.failed.tooLarge`).
- At most 20 GB (`20_000_000_000` bytes) total uncompressed size per archive,
  checked across directory entries and cumulative extracted bytes
  (`import.zip.failed.tooLarge`).
- Any entry with uncompressed-to-compressed ratio above 100:1 refuses the
  archive (`import.zip.failed.tooLarge`), both in directory metadata and while
  inflating raw deflate streams in bounded chunks.
- Encrypted archives (traditional PKWARE, strong encryption `0x0017` or WinZip
  AES method `99` / extra `0x9901`, in central or local headers) are refused
  without prompting (`import.zip.failed.encrypted`).
- Leading `/` or `\`, `..` components, Windows drive prefixes (`^[A-Za-z]:`),
  NUL bytes or special file modes refuse the whole archive before importing
  anything (`import.zip.failed.unsafePaths`). Valid filenames containing `:`
  and Windows relative paths using `\` separators are normalized and extracted
  by bounded basename into a private `0700` temporary directory with `0600`
  files while preserving their archive folder hint.
- ZIP64 end-record `0xFFFF` / `0xFFFFFFFF` sentinels and local-vs-central
  general-purpose bit flag differences are accepted. Directory entries count
  toward the 2,000-entry directory cap without inflating file or unsupported
  counts. macOS `__MACOSX/` and `._` AppleDouble sidecars, nested archives and
  unsupported formats/methods are never opened and are counted in
  `import.summary.unsupported`; an archive with no supported Opus entry reports
  `import.zip.failed.empty`; unreadable or overlapping-header archives report
  `import.zip.failed.damaged`. Any whole-archive runtime limit violation rolls
  back notes staged from that archive so nothing from a refused archive remains.
- Batch deduplication indexes existing `Received-*` SHA-256 metadata once per
  batch ($O(M + N)$) while still re-verifying full file bytes on every digest
  match, preserving committed notes on cancellation or later member failure.

## Bulk stored-`.zip` export (D96)

The Library primary menu exposes `export.zip.all` (`win.export-zip`) whenever at
least one saved audio meeting outside Trash is available and no bulk export is
running. Opening it presents an explicit selection dialog initialized with all
eligible meetings checked (keeping bulk selection separate from row Open/preview
and pruning trashed or removed meetings live on Library updates). Users can
choose either `export.zip.all` or, while one or more rows are checked,
`export.zip.selected`, then pick the destination folder through the native
folder chooser (`export-folder-uri` is remembered as the initial folder; Flatpak
rejects unbacked private-home paths before staging anything).

Bulk export runs on a dedicated single-worker `ThreadPoolExecutor` with
`export.zip.progress` (`done`, `total`) and `common.cancel`, and publishes
`Cassini.zip` (or `Cassini 2.zip` on collision) atomically with directory
`fsync`, followed by the `export.zip.done` plural toast and `common.show`:

- Every archive entry uses `ZIP_STORED` (`compress_type == 0`) at the root of
  the archive (no extra manifest file), with safe bounded UTF-8 entry names
  (stripping leading dots and normalizing drive-like prefixes so exported names
  always pass cross-platform path validation) and case-insensitive numeric
  collision suffixes.
- Enforces the same `MAX_ENTRIES = 2,000` and `MAX_BYTES = 20_000_000_000`
  bounds during export and preflights the staged archive before publication.
- Stages at most one meeting snapshot at a time next to the destination (never
  `/tmp` PCM) and unlinks each temporary snapshot immediately after streaming it
  into the `.zip`.
- Unedited received Cassini `.opus` documents and plain imported `.opus` audio
  files without a Cassini document yet are copied byte-for-byte so plain audio
  is never left behind and re-importing an unedited archive deduplicates by
  SHA-256 (`imported == 0`). Own recordings and locally edited meetings export
  verified v1 Cassini `.opus` documents with internal Library extensions
  stripped. Cancelling mid-write removes incomplete staged files without showing
  an error toast.
