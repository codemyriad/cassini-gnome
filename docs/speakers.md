# Naming and merging speakers (UX16)

The Meeting sidebar opens the native Speakers dialog for a chosen speaker. Its
speaker list scrolls, keeping the last speaker and naming action reachable without
stretching the window for a large meeting.
Names are trimmed, limited to 64 characters and contain no control or formatting
characters. An empty field stores `name: null, provenance: unnamed`. This is an
explicit local override of a received file's embedded name; without a local
speaker entry the embedded label is still inherited. Source names such as Me and
The others can also be changed.

All metadata reads and writes run on the Library worker. The retained scan cache
keeps unchanged received audio from being re-read or hashed on an edit. Before an edit, the
writer resolves the configured Library and reloads the current meeting. Outside
folders and symlinks are rejected. A successful edit publishes one atomic
meeting.json revision, retaining other speaker fields and unrelated metadata.
Failure retains the entered name and previous metadata and offers Try again.
Cancel and closing the dialog discard unsaved text. Playback shortcuts are
suspended while the name field has focus.

`version` remains integer 1. The optional `speakerMerges` array is an ordered
history: A→B followed by B→C displays both as C. Writers require two different
currently visible speakers. Readers ignore unknown IDs and entries that would
create a cycle, then resolve chains to their final targets. Undo merge removes
the last entry, restoring A→B in this example. Original transcript items are
never rewritten. The resolved Library model feeds transcript rows, speaker
numbering, Search and both export formats.

The Library scan computes the union of each speaker's word spans for talk time.
It also chooses the longest run of consecutive items for the sample, breaking
ties by earliest start, and limits the sample to 10 seconds. GStreamer enforces
the stop boundary on output PCM; a UI timer is not responsible for limiting the
audio. Samples wait for preroll and remain paused if the bounded seek is rejected. Stopping a sample restores the previous position, paused. Normal playback
clears the range so it can play the whole meeting again.

Saved voices, recognition and Remember this voice remain outside UX16. The
`speakers` screenshot board uses the shared Client call fixture and actual word
timings, with Giulia entered but unsaved. Its 11-second talk time is calculated.

`tests/test_speakers.py` exercises edit persistence, validation, merge history,
Undo, malicious entries, read-only ownership and resolved Search/export output.
The real GStreamer tests count output samples for both file and meeting-folder
playback, including the 10-second cap and recovery to full playback. The private
GTK test covers validation, failed save/retry, name/clear, sample, merge/Undo,
Cancel and read-only controls in both themes. All test audio uses appsink or
fakesink, and GUI tests use private displays, buses and temporary Libraries.
