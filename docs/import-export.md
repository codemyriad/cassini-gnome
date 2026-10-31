# Received import and two-format export (W4-2)

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

Export has exactly Cassini file (default) and Markdown, through the header, menu
and Ctrl+Shift+E. The old Ctrl+E/TXT/WebVTT paths and serializers are removed.
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
