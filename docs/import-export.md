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
retain the existing failed-open presentation until that copy is published.

Export has exactly Cassini file (default) and Markdown, through the header, menu
and Ctrl+Shift+E. The old Ctrl+E/TXT/WebVTT paths and serializers are removed.
The shared dialog discloses every automatically recognised name, with only the
names semibold, no Oxford comma and no truncation; long content scrolls. Choose
uses the native folder chooser, Documents is the initial destination, Cancel
cancels a background job. Output files publish without replacing an existing
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
