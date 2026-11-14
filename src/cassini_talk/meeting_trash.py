# SPDX-License-Identifier: GPL-3.0-or-later
"""Move to Trash, Undo, Restore and purge for the Library (HARMONY §7, §7.1; D100, D-1032).

A deleted meeting moves at once, whole and by identity, into the Library's own
`.cassini/trash/<entry-id>/` (S-12 §7.1, library_v2.trash): no system Trash and no Trash
portal, so Undo and Restore work the same inside the Flatpak. The toast's Undo is a Restore
of that entry (nothing waits for the toast to close). Entries expire after 30 days, checked
at launch and hourly. All file work runs on one worker thread, in request order.
"""

import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Adw, Gio, GLib

from . import library_location as location
from .library_publication import root_for
from .library_v2 import journal
from .library_v2 import trash as storage
from .meeting_state import MeetingError
from .strings import text
from .trash_cleanup import TrashCleanup

LOG = logging.getLogger(__name__)
EXPIRY_SECONDS = 3600


def menu_item():
    item = Gio.MenuItem.new(text("common.moveToTrash"), "win.move-to-trash")
    # Display the scoped shortcut without installing an application-wide Delete accelerator.
    item.set_attribute_value("accel", GLib.Variant("s", text("common.shortcut.moveToTrash")))
    return item


def _utc_text(at_ms):
    if at_ms is None:
        return None
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def representations(meeting):
    """(Library root, published root name or None, working folder root-relative path or None)."""
    directory = Path(meeting.directory).absolute()
    published = meeting.published_file
    if published is not None:
        published = Path(published).absolute()
        if published.is_symlink():
            raise ValueError("Meeting file is a link")
        root = published.parent.resolve()
        session = None
        if directory != published:
            with contextlib.suppress(ValueError):
                if root_for(directory) == root and directory.is_dir() and not directory.is_symlink():
                    session = f"{storage.SESSIONS}/{directory.name}"
        return root, published.name, session
    managed = root_for(directory)
    if managed is not None:
        return managed, None, f"{storage.SESSIONS}/{directory.name}"
    if directory.is_symlink():
        raise ValueError("Meeting folder is a link")
    return directory.parent.resolve(), None, directory.name


def _owned_root(root):
    allowed = {Path(r).resolve() for r in location.roots()}
    if Path(root).resolve() not in allowed:
        raise ValueError("Meeting is outside the configured Library")
    return Path(root).resolve()


def _stored_prefix(path):
    """The file's persisted `x["codemyriad.io"].namePrefix`, or None."""
    from . import portable
    from .library_v2 import xschema

    with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
        manifest = portable.read_meeting(str(path)).manifest or {}
        ours = (manifest.get("x") or {}).get(xschema.DOMAIN) or {}
        prefix = ours.get("namePrefix")
        return prefix if isinstance(prefix, str) else None
    return None


def trash_meeting(meeting, *, queue=None):
    """Worker only: move one meeting into the Library's trash. Returns (root, entry id)."""
    from . import meeting_store

    directory = Path(meeting.directory).absolute()
    exclusion = queue.cancel_meeting(directory) if queue else contextlib.nullcontext()
    with exclusion, location.routing():
        root, published, session = representations(meeting)
        root = _owned_root(root)
        busy = meeting.origin == "own" and location.capture_busy(meeting.id)
        entry_id = storage.move_to_trash(
            root,
            published=published,
            session=session,
            title=meeting.title,
            recorded_at_utc=_utc_text(meeting.started_at_ms),
            duration_ms=meeting.duration_ms,
            name_prefix=_stored_prefix(root / published) if published else None,
            received=meeting.origin == "received",
            active_lease=busy,
            guard=lambda: meeting_store.LOCK,
        )
    return root, entry_id


def forget_purged(voices, root, identities):
    """After a purge: drop this device's private per-meeting state (routing aliases and voice
    prints of that meeting). Saved voices themselves are never unenrolled."""
    for identity in identities:
        folder = (identity or {}).get("session")
        if not folder:
            continue
        for path in (Path(root) / storage.SESSIONS / folder, Path(root) / folder):
            with contextlib.suppress(OSError, ValueError):
                location.forget_meeting(path)
        if voices is not None:
            with contextlib.suppress(OSError, ValueError):
                voices.remove_meeting(Path(root) / storage.SESSIONS / folder)


@dataclass
class PendingTrash:
    meeting: object
    window: object
    toast: object = None
    phase: str = "moving"  # moving -> done | failed; then restoring -> restored | done
    root: Path | None = None
    entry_id: str | None = None
    undo: bool = False


class MeetingTrash:
    def __init__(self, app):
        self.app = app
        self.operations = {}  # Path -> PendingTrash while its move or restore runs
        self.hidden = {}  # Path -> time its move finished, or None while it runs
        self.absent = set()
        self.errors = {}
        self.toast = None
        self.waiters = []
        self.failed_flush = False
        self.listeners = []  # callables run on the GTK thread after any trash change
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-trash")
        self.cleanup = TrashCleanup(app.voice_store)
        self.executor.submit(self.cleanup.retry)  # Phase 1 (D29) bookkeeping still queued
        self.executor.submit(self._maintain).add_done_callback(self._maintained)
        self._timer = GLib.timeout_add_seconds(EXPIRY_SECONDS, self._hourly)

    # ---- Library projection ------------------------------------------------------------

    def hides(self, directory):
        return Path(directory).absolute() in self.hidden

    def allowed(self, meeting, window=None):
        return bool(
            meeting
            and meeting.lifecycle == "saved"
            and not self.hides(meeting.directory)
            and not self.app._quitting
            and not (self.app.library_relocator and self.app.library_relocator.running)
            and (window is None or not (window.search_page or window.recording_page or window._external_meeting))
        )

    def project(self, snapshot):
        meetings = tuple(
            replace(m, error=MeetingError("trash-" + self.errors[m.directory], "Try again"))
            if m.directory in self.errors
            else m
            for m in snapshot.meetings
            if not self.hides(m.directory)
        )
        if meetings == snapshot.meetings:
            return snapshot
        return replace(
            snapshot, meetings=meetings, directories=tuple(p for p in snapshot.directories if not self.hides(p))
        )

    def scanned(self, snapshot, started):
        if snapshot.unavailable:
            return
        present = {m.directory for m in snapshot.meetings}
        for directory, ended in list(self.hidden.items()):
            if ended is None or started <= ended:
                continue
            if directory not in present:
                self.absent.add(directory)
            elif directory in self.absent:
                # Restored from the Trash view (or a new meeting took this path): show it.
                self.hidden.pop(directory)
                self.absent.discard(directory)

    # ---- Move to Trash and Undo --------------------------------------------------------

    def request(self, meeting, window):
        if not self.allowed(meeting, window):
            return
        if self.toast:
            self.toast.dismiss()  # Only the newest move offers Undo.
        directory = meeting.directory.absolute()
        self.errors.pop(directory, None)
        operation = PendingTrash(meeting, window)
        self.operations[directory] = operation
        self.hidden[directory] = None
        waits = self._prepare(meeting)
        toast = operation.toast = Adw.Toast(
            title=GLib.markup_escape_text(text("meeting.trashedToast", title=meeting.title)),
            button_label=text("common.undo"),
            timeout=5,
        )
        toast.connect("button-clicked", lambda *_: self.undo(operation))
        toast.connect("dismissed", lambda *_: self._toast_closed(operation))
        self.toast = toast
        self._refresh()
        window.toasts.add_toast(toast)
        self.app.hold()
        queue = self.app.transcriber

        def work():
            for future in waits:
                with contextlib.suppress(Exception):
                    future.result()  # Drain finished/failed writers; their own UI reports the error.
            return trash_meeting(meeting, queue=queue)

        future = self.executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(self._moved, operation, f))

    def _toast_closed(self, operation):
        if self.toast is operation.toast:
            self.toast = None

    def _prepare(self, meeting):
        waits = []
        for window in list(self.app.get_windows()):
            page = getattr(window, "meeting_page", None)
            if page and page.meeting.directory == meeting.directory:
                if page._speaker_future:
                    waits.append(page._speaker_future)
                speakers = page.speakers_dialog
                if speakers and speakers.future:
                    waits.append(speakers.future)
                window.back_to_library()
            if getattr(window, "_loaded_directory", None) == meeting.directory:
                window._stop_playback(clear_selection=True)
            dialog = getattr(window, "export_dialog", None)
            if dialog and dialog.meeting.directory == meeting.directory:
                if not dialog.closed:
                    dialog.cancel()
                if getattr(dialog, "future", None):
                    waits.append(dialog.future)
            zip_dialog = getattr(window, "zip_export_dialog", None)
            if zip_dialog and meeting.directory.absolute() in getattr(zip_dialog, "active_directories", ()):
                if not zip_dialog.closed:
                    zip_dialog.cancel()
                if getattr(zip_dialog, "future", None):
                    waits.append(zip_dialog.future)
            if getattr(window, "snapshot", None):
                values = list(window.snapshot.meetings)
                index = next((i for i, value in enumerate(values) if value.key == meeting.key), None)
                if index is not None:
                    following = values[index + 1 : index + 2] or values[max(0, index - 1) : index]
                    window._pending_selection = following[0].key if following else None
        settings = self.app.settings_dialog
        samples = settings.saved_voices_page if settings and not settings.closed else None
        if samples:
            samples.discard_meeting(meeting.directory)
        if self.app.comfort:
            waits.append(self.app.comfort.editor.submit(lambda: None))
        return tuple(waits)

    def _moved(self, operation, future):
        directory = operation.meeting.directory.absolute()
        try:
            operation.root, operation.entry_id = future.result()
        except Exception as error:
            LOG.warning("Could not move meeting to Trash: %s", error)
            operation.phase = "failed"
            self.failed_flush = True
            self.hidden.pop(directory, None)
            self.errors[directory] = "failed"
            if operation.toast:
                operation.toast.dismiss()
            operation.window._pending_selection = operation.meeting.key
            operation.window.present()
            self._finish(operation)
            return GLib.SOURCE_REMOVE
        operation.phase = "done"
        self.hidden[directory] = time.monotonic()
        self.app.discard_meeting_paths(directory)
        if operation.undo:
            self._restore_for_undo(operation)
        else:
            self._finish(operation)
        return GLib.SOURCE_REMOVE

    def window_hidden(self, window):
        """Nothing waits for a toast any more: the move already happened (D100)."""
        if self.toast and any(o.toast is self.toast and o.window is window for o in self.operations.values()):
            self.toast.dismiss()

    def undo(self, operation):
        if operation.undo or operation.phase not in ("moving", "done"):
            return
        operation.undo = True
        if self.toast is operation.toast:
            self.toast = None
        if operation.phase == "done":
            directory = operation.meeting.directory.absolute()
            self.operations[directory] = operation
            self.app.hold()
            self._restore_for_undo(operation)

    def _restore_for_undo(self, operation):
        operation.phase = "restoring"
        root, entry_id = operation.root, operation.entry_id
        future = self.executor.submit(storage.restore, root, entry_id)
        future.add_done_callback(lambda f: GLib.idle_add(self._undone, operation, f))

    def _undone(self, operation, future):
        directory = operation.meeting.directory.absolute()
        try:
            future.result()
        except Exception as error:
            LOG.warning("Could not restore meeting from Trash: %s", error)
            operation.phase = "done"
            operation.window.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text("trash.restoreFailed"))))
        else:
            operation.phase = "restored"
            self.hidden.pop(directory, None)
            self.absent.discard(directory)
            operation.window._pending_selection = operation.meeting.key
        self._finish(operation)
        return GLib.SOURCE_REMOVE

    def _finish(self, operation):
        directory = operation.meeting.directory.absolute()
        if self.operations.get(directory) is operation:
            self.operations.pop(directory)
        self._changed()
        if not self.operations:
            callbacks, self.waiters = self.waiters, []
            failed, self.failed_flush = self.failed_flush, False
            for callback, on_failure in callbacks:
                notify = on_failure if failed else callback
                if notify is not None:
                    notify()
        self.app.release()

    def flush(self, callback, *, failed=None):
        """On close/quit: keep GTK alive until running moves and restores have finished."""
        if not self.operations:
            return False
        waiter = (callback, failed)
        if waiter not in self.waiters:
            self.waiters.append(waiter)
        return True

    # ---- the Trash view's operations ---------------------------------------------------

    def root(self):
        return Path(location.primary())

    def submit(self, work, done, *, changes=True):
        """Run `work` on the trash worker; `done(result, error)` on the GTK thread.

        `changes=False` marks a read-only job (listing the entries): it never notifies the
        listeners, since the Trash view reloads on every notification (no refresh loop)."""
        self.app.hold()

        def finished(future):
            def report():
                try:
                    result, error = future.result(), None
                except Exception as exc:  # reported by the view
                    result, error = None, exc
                try:
                    done(result, error)
                finally:
                    if changes:
                        self._changed()
                    self.app.release()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(report)

        self.executor.submit(work).add_done_callback(finished)

    def entries(self, done):
        self.submit(lambda: storage.entries(self.root()), done, changes=False)

    def restore(self, entry_id, done):
        root = self.root()
        self.submit(lambda: storage.restore(root, entry_id), done)

    def purge(self, entry_id, done):
        root, voices = self.root(), self.app.voice_store

        def work():
            identity = None
            with contextlib.suppress(OSError, ValueError):
                identity = next((e.identity for e in storage.entries(root) if e.entry_id == entry_id), None)
            storage.purge(root, entry_id)
            forget_purged(voices, root, [identity])

        self.submit(work, done)

    def empty(self, done):
        root, voices = self.root(), self.app.voice_store

        def work():
            try:
                purged = storage.empty_trash(root)
            except storage.Incomplete as error:
                forget_purged(voices, root, error.purged)  # what did go is forgotten; the rest stays
                raise
            forget_purged(voices, root, purged)

        self.submit(work, done)

    # ---- startup recovery and 30-day expiry --------------------------------------------

    def _maintain(self):
        """Worker: finish interrupted moves forward (S-12 §7.1 recovery), then expire.
        Returns True when anything changed, so the Library rescans only then."""
        voices = getattr(self.app, "voice_store", None)
        changed = False
        for root in dict.fromkeys(Path(r) for r in location.roots()):
            try:
                if not (root / journal.META).is_dir():
                    continue
                if journal.pending(journal.Layout(root), cleanup=False).entries:
                    journal.recover(root)
                    changed = True
                purged = storage.expire(root)
                forget_purged(voices, root, purged)
                changed = changed or bool(purged)
            except (OSError, ValueError, journal.LockBusy):
                LOG.exception("Trash maintenance for %s will be retried", root)
        return changed

    def _maintained(self, future):
        with contextlib.suppress(Exception):
            if future.result():
                GLib.idle_add(self._changed)

    def _hourly(self):
        if self.app._quitting:
            self._timer = None
            return GLib.SOURCE_REMOVE
        self.executor.submit(self._maintain).add_done_callback(self._maintained)
        return GLib.SOURCE_CONTINUE

    def _changed(self):
        self._refresh()
        for window in self.app.get_windows():
            if hasattr(window, "monitor"):
                window.monitor.refresh_now()
        for listener in list(self.listeners):
            listener()
        return GLib.SOURCE_REMOVE

    def _refresh(self):
        for window in self.app.get_windows():
            snapshot = getattr(window, "_source_snapshot", None)
            if snapshot is not None:
                window._updated(snapshot)
