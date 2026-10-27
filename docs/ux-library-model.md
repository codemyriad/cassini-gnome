# Library model — D6/D6.1 and S-3a

The pure Python scanner implements HARMONY §§3–4/6 and the binding Library
rules R1–R10, including D6.1. `LibraryScanner(root).scan(capture_activity=...)`
reads literal `~/Music/Cassini` by default; an explicit root or
`CASSINI_LIBRARY_DIR` overrides it. There are no Library widgets or startup
changes in this PR. Capture/app-owned source files are never written.

Canonical lowercase folder UUIDs only; meeting symlinks are skipped. The root
may be a link; absent is empty and dangling/unreadable is failed. All interior
reads use directory-relative `O_NOFOLLOW|O_NONBLOCK`, regular-file checks and
opened-file stat keys. A FIFO cannot block a scan. A Recording stays listed when
its contents are unreadable; Received requires a fully valid published sidecar
with a non-null received object. UUID remains `id`; folder basename is the
internal key and last sort tie-break, allowing the same UUID under both prefixes.

App metadata is validated all-or-nothing against the verbatim vendored schema,
plus UUID/context/unique-speaker checks. The small stdlib validator supports this
schema's vocabulary and has differential tests against jsonschema; no new runtime
dependency is needed. D6.1's schema accepts null ETA for queued/running, so there
is no local validation exception. Newer metadata is retained but ignored for
display and treated read-only. Ignored/absent own metadata falls back to a regular
transcript.json. Unknown fields in supported metadata are retained.

The stored status wins: pending jobs survive missing bodies, failed jobs retain
or derive Try again, done bodies must be readable, none is never promoted.
Actionable errors survive damage. Explicit embedded IDs/file names are honoured;
missing IDs never select a different transcript. Received audio integrity is
checked once per changed file; stale/unknown-format bodies are withheld while
actual audio duration is read independently from the final EOS granule/pre-skip.

Capture activity is injected, not inferred from mtime. Only R4's interruption
conditions add history; completed sessions remain completed despite per-track
problems. Unknown/invalid source offsets stay None and count as zero for display
only. End positions use exact rational arithmetic and floor the full sum per
D6.1. No counted track means unknown duration/history, with no invented zero.
Title fallbacks, unknown dates, ordering, speaking-only numbering/colours, status-
gated quotes, consecutive turns and whitespace/override rules follow D6.

`scan(changed_directories={...})` updates just those folders; omitted means a root
rescan. Parsed JSON, sidecar/embedded words, Opus headers/digests, WAV headers and
unchanged derived rows are cached by device/inode/size/mtime_ns. The scanner lock
serializes cache access. No cache is authoritative or written into the Library.
A cold scan still checks each received file's integrity, as R3 requires; this is
worker I/O, not UI-thread work.

`LibraryMonitor` keeps existing watches, including unpublished Received folders,
uses trailing-edge 250ms debounce capped at 2s, and runs scans in one worker.
Complete snapshots return through GLib's main context. Metadata events rescan
only their meeting; root events rescan the root. WAV growth is ignored; live
recording presentation uses controller activity/session metadata updates.
`refresh()` explicitly reloads activity, and `close()` suppresses late delivery.
Tests prove responsiveness while the worker is blocked, incremental reads, cache
hits after metadata edits, stable watches, bounded debounce and close-in-flight.

Shared inputs are copied verbatim with SHA-256 and coordinator source revision in
`tests/ux/UPSTREAM.json`. They include format v3 (69 cases), states v2 (17), the
schema/rules and S-3a's independent hand-authored Library suite (13). The generator
and expected files live in `ux/library-vectors/`; the runner compares exact JSON
and CI uploads every actual case, JUnit and explicit pass counts. The same check
script supports `--source /path/to/cassini-coordination/ux-draft` byte comparison.
No separate contract repository is planned (D7).

Out of scope: Library UI, import/deduplication, metadata writers/locks, recording
controller, playback/mixdown, transcription, export and the later disk/Settings/
voices decisions. The existing browser-meeting UI remains unchanged.
