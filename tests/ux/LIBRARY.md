# Library layout draft, v1 (W1-M1)

HARMONY §§3–4 is authoritative. Both apps use literal `~/Music/Cassini`; tests inject another
root. Scanning never creates, repairs or rewrites the Library. No recording save dialog.

```
Music/Cassini/
  Recording-<uuid>/
    session.json, track-*.wav      # capture-owned; names come from session.json
    meeting.json                  # app-owned, schema alongside this document
    transcript.json               # optional app-owned cassini.words.v1 payload
    .meeting.lock                 # app metadata writer lock, never removed
  Received-<uuid>/
    <original basename>.opus       # original bytes, immutable
    meeting.json                  # app-owned; received.originalName identifies that file
    transcript.json               # optional, if written down locally once
```

Both generated track-<uuid>.wav and named track-microphone.wav / track-system.wav are legal;
all use the same PCM16 header. The safe track-*.wav basenames in session.json are the record.

UUID is the Library identity, matching `meeting.json.id` and (when readable) `session.json.id`.
Only direct children with these prefixes and valid UUIDs are meetings. All file references are
single basenames; reject traversal, absolute paths and escaping symlinks. Voiceprints are
NEVER here: app-data `cassini/voices` on GNOME; Application Support/Cassini/voices on macOS.

## meeting.json

UTF-8 JSON, `version:1`, `revision:0` initially; ≤1 MiB. Unknown fields are preserved on edit.
The schema defines title/titleSource, notes, speakers, transcript, etaMinutes, error,
quoteOverride and received. `titleSource` is `meeting-window` or `user`; null title delegates
to capture/embedded metadata. A user title is nonempty. Notes/quotes store plain text, not
rendered typographic quotation marks. Speaker ids are unique; names/provenance are independent
of source ids. Writers assign colourIndex from zero in order of first speaking.

Transcript reference is null, `{kind:"sidecar",file:"transcript.json",format:"cassini.words.v1"}`,
or `{kind:"embedded",file:"<original>.opus",transcriptId?:"…"}` (default entry if omitted).
A sidecar is the existing cassini.words.v1 body: language plus items containing speaker,
startMs, endMs, text; optional lowConfidenceSpeaker/attributionGapDb. It is atomically replaced,
not appended in place. The optional `transcript.model` records the speech model that produced it: `"whistle"` or
`"parakeet-ultra-0.6b-int8@<source_sha256>"` (D16). Absent means unknown. No engine, writer/export or transcript generation is assigned in wave 1.

`received` is null for own meetings, otherwise originalName, optional originalFolderName
(display basename only), lowercase full-file sha256 and importedAtMs. Import copies original
bytes before publishing meeting.json; a repeated full-file SHA identifies a duplicate (UX4).
For legacy received labels without naming provenance, readers retain the label with unknown
provenance in their derived model; do not invent automatic recognition or "named by you".

## Field sources (HARMONY §3)

| Model field | Source / derivation |
|---|---|
| lifecycle | Capture activity observation, then capture files; see below. Received is saved. |
| origin | Recording-* → own; Received-* plus received metadata → received. Not stored twice. |
| interruptedAtMs | Capture interrupted/recovered/unfinalized state or counted capture gap; maximum `(startOffsetMs ?? 0) + complete WAV frames/rate`, for display only (D3). Never meeting.json. |
| transcript | meeting.json transcript.status; infer done/none/damaged from referenced/embedded body if metadata is absent or body unreadable. Missing/damaged data cannot claim done. |
| etaMinutes | meeting.json, nonnegative only for queued/running, otherwise null. |
| error | meeting.json `{id,action}`; unreadable transcript derives transcript-damaged/null. No raw exception text. |

Current capture v1 persists recording/completed/interrupted, but no saving phase or writer
liveness lease. An injected capture-activity adapter may report recording/saving for a known
active UUID (future controller/broker integration). Without such evidence an unfinalized
recording manifest is recovered as saved/interrupted, never guessed live from mtime. This
conservative legacy rule and the adapter requirement are adopted in DECISIONS-UX.md D2; wave 1 does not change capture writers or invent capture-owned fields.
Completed capture is saved. Interrupted history remains independent of transcript progress.
A capture's nonzero gapFrames/problemKind=gap also preserves interruption history; generic
permission/empty/IO problem text alone is not copied into a new user-facing error line.

Title prefers app metadata, then capture/embedded title. Start prefers capture createdAt or
embedded createdAtUtc, else received importedAtMs; absent/corrupt own dates remain unknown.
Duration uses complete PCM frames plus source offsets (maximum end), or the original Opus
sample duration. Per D3, unknown source starts count as zero ONLY for display duration and
interruptedAtMs; retain an internal recovery diagnostic. Never write that assumed zero back
or use it for playback alignment/synchronisation. Such meetings keep a normal Library row.
Speaker names sort by earliest speaking time (file order breaks ties); unnamed is Speaker n.
Quote prefers quoteOverride (last played/searched turn); otherwise the longest-duration turn
starting in the first 600000 ms, with earliest start/file order breaking ties. No transcript,
no quote. Derived/cache data never makes an unreadable capture disappear from the scan.

## Atomic edits and concurrent apps

Both apps acquire an exclusive POSIX flock on the SAME persistent `.meeting.lock` inode,
then read current meeting.json and compare the caller's expected revision. A stale revision
returns a conflict for reload/reapply; never silently overwrite the other app's edits.
Increment revision and write `.meeting-<uuid>.tmp` mode 0600 in the SAME directory; flush,
rename to meeting.json atomically, then sync the directory. Hold the lock through the commit.
Never unlink the lock file; process exit releases the lock. Unsupported locking fails the
edit. New meeting directories are 0700; original/capture files are never edited by this path.
Readers need no lock and see either complete revision. Ignore abandoned temp/lock files.
All app writers (including transcript jobs) use this protocol. Unknown newer versions are
read-only/unsupported, never destructively rewritten. Capture files remain capture-owned.

Each app may build its own rebuildable index/search cache in app data. Deleting that cache
must not delete or rename a meeting or change metadata. No authoritative SQLite/index in
Music/Cassini. A missing root is an empty Library; a failed scan is distinct from empty.
