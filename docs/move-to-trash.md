# Move to Trash (D29)

Library row menus and the Meeting menu offer **Move to Trash** for saved Library
meetings. Delete applies to a focused Library list or Meeting; entries and text
views keep their normal editing keys. Search, external files, recording/saving
and Library moves do not offer the action.

The selected row disappears immediately. A native AdwToast uses its default
five-second timeout and Undo. No file moves until dismissal, timeout, replacement
or closing/quitting. Undo restores the row and its selection. Actual removal is
`Gio.File.trash`, on a worker, never permanent deletion.

Before removal, playback and samples stop, the Meeting and Export dialog close,
and pending exports/metadata edits finish. `TranscriptionQueue.cancel_meeting`
cancels just this folder's preparation, queued/running speech and voice jobs. Its
exclusion remains active through Trash and private cleanup; other meetings keep
running. File ownership and capture leases are rechecked under routing/metadata
locks. Those locks are released before acquiring the private voice-store lock.

After success, only this meeting's private print file is removed transactionally.
Saved people and their enrolled centroids/seconds/counts remain. Meeting links,
pending Library moves, completed capture paths and hidden-window drafts are
cleared. Search uses SQLite secure deletion. A later filesystem scan recognises
a system-Trash restore without rewriting its meeting metadata.

A private `trash-pending.json` intent is durable before the native call. If Trash
fails it is discarded and the row returns with the published warning. If Trash
succeeds but private bookkeeping fails, the row remains removed and the intent
retries on launch; it never falsely says that the folder is still in the Library.
An unavailable drive is not treated as a removed folder. The journal is local
app data, not a Library or shared-format change.

Checks cover real child cancellation, queued-job isolation, original-byte native
Trash, private rollback/retry, pending moves, hidden-save guards and search-cache
text removal. `tests/ui/trash.py` exercises real GTK menus, text-safe Delete,
Undo, the real timeout, replacement, failure, restoration and quit. Screenshots
read only the private Xvfb screen so native popup surfaces are included.
`tests/flatpak/trash.py` installs no grants: the installed sandbox uses GIO and
the real host [Trash portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Trash.html)
on a private bus, then checks original bytes in the private host Trash and that
a new sandbox launch still sees an empty Library.
