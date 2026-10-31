# Moving a published meeting

GNOME's existing D15 routing journal owns cross-Library moves. The shared S-12
journal still owns file publication, metadata edits and Trash. No new field is
written into a Cassini file or its shared metadata schema for a move.

A pending entry may carry a `pair` checkpoint. Its phases are `copying`, `staged`,
`published` and `retired`. The checkpoint records the operation UUID, root-file
basename, source file stat, source session-tree fingerprint, audio digest,
revision, and staged inode identities. A phase is appended and fsynced before
any following destructive step. One process holds the move-worker lease;
producers never acquire that lease.

Copying and verification run outside the locks. Closed audio uses same-inode
links when available; cross-filesystem copies stream and verify SHA-256. JSON is
copied. Commit takes routing, both Library locks in path order, then the metadata
lock. Library acquisition is nonblocking: contention releases both and defers
the move, avoiding lock-order deadlocks with another platform. It replays pending shared operations and rechecks source identity/content,
source activity, destination freedom and the routing entry before publishing.
No-replace renames publish the file and optional session. Only after both are
durable does the checkpoint make the destination authoritative. The originals
then move to private retired names before cleanup. The collection hides partial
destinations and keeps exactly the authoritative row. An editor queued against
the old Library lock resolves routing again after acquiring it.

A retry verifies a previously published destination outside the locks, then
rechecks its stat under the locks. A newer destination edit/rename is retained.
If it happened between file and session publication, the incoming working
metadata is rebuilt from the newer file before the session is published. An
unrelated collision is never overwritten. An unpublished stage is disposable;
a partially published pair and unresolved copies remain journaled on error.

The crash tests SIGKILL spawned movers at each durable boundary using a pipe
readiness notification and a signal wait. Tests also cover an absent session
cache, retained destination edits, an editor queued on the old lock, source
edits during copying, collisions, cancellation and cross-filesystem fallback.
Unowned root files remain pending the shared adoption API (coordination R55).
