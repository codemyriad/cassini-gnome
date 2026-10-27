# W1-G2 Library model

This PR implements HARMONY §§3–4/6 and coordinator decisions D1–D3 without
changing the UI. `library.py`, `meeting_state.py` and `ux_formats.py` are pure
Python. `library_monitor.py` is the separate GFileMonitor adapter.

`LibraryScanner(root).scan(capture_activity={uuid: 'recording' | 'saving'})`
reads direct `Recording-<uuid>` and `Received-<uuid>` children. Without an injected
root it uses literal `~/Music/Cassini`; `CASSINI_LIBRARY_DIR` overrides this for
tests. Missing root is empty; unreadable root has `unavailable=True`. Individual
unreadable meetings remain in the snapshot with stable folder identity and
internal diagnostics. No import, metadata edit, cache file or capture repair is
performed. Unknown app metadata versions and fields remain untouched.

Capture metadata and physical complete PCM frames determine duration/history.
An unfinalized capture without a live activity report is saved/interrupted,
regardless of mtime. Named native WAVs and UUID browser WAVs are both accepted.
Missing offsets stay `None` on `CaptureTrack`; D3 uses zero only for the derived
display duration and interruption position, retaining a recovery diagnostic.
It does not claim playback alignment. No source label becomes a speaker name.

Received originals follow S-1's `received.originalName`. The existing portable
reader supplies embedded metadata/words; streaming Opus inspection supplies the
actual sample duration. An in-memory cache avoids re-reading unchanged originals
and can be discarded without changing the Library. App sidecar words, speaker
names/provenance and quote overrides are read without writes. Names use earliest
speaking time, and the fallback quote is the longest turn starting in the first
ten minutes. Legacy embedded names retain unknown provenance.

`LibraryMonitor(callback, root, capture_activity=callable)` watches root creation,
meeting folders and atomic JSON changes; `refresh()` also handles activity-source
updates. `close()` cancels pending callbacks. It is not wired into any widget in
this milestone. A future controller can schedule scans off its UI thread.

Golden files under `tests/ux/` are verbatim coordinator copies. `UPSTREAM.json`
records their SHA-256 values and source revision. The check script verifies these
bytes offline; `--source /path/to/cassini-coordination/ux-draft` additionally
compares against the live coordinator files. CI has no SSH access to DrMeyers,
so it verifies the reviewed pin/hashes and runs every case. The new workflow
uploads JUnit and explicit format/state pass counts.

Out of scope: Library widgets, import/deduplication, sidecar writers/locks, fixture
generation (S-3 is not assigned yet), playback/mixdown, transcription, export,
recording-controller integration and all later UX milestones.
