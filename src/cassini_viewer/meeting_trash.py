# SPDX-License-Identifier: GPL-3.0-or-later
"""D29 deferred native Trash, scoped job exclusion and Library presentation."""

import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from gi.repository import Adw, Gio, GLib

from . import library_location as location
from .library import LibraryScanner, meeting_id
from .meeting_state import MeetingError
from .meeting_store import LOCK
from .strings import text
from .trash_cleanup import TrashCleanup

LOG = logging.getLogger(__name__)


def menu_item():
    item = Gio.MenuItem.new(text("common.moveToTrash"), "win.move-to-trash")
    # Display the scoped shortcut without installing an application-wide Delete accelerator.
    item.set_attribute_value("accel", GLib.Variant("s", text("common.shortcut.moveToTrash")))
    return item


def trash_owned(directory, *, queue=None, voices=None, trash=None, cleanup=None):
    """Worker only: never permanently delete or operate outside the routed Library."""
    exclusion = queue.cancel_meeting(directory) if queue else contextlib.nullcontext()
    with exclusion:
        with location.routing(), LOCK:
            directory = location.owned_directory(directory)
            if directory.exists():
                if location.capture_busy(meeting_id(directory)):
                    raise ValueError("Meeting is still recording or saving")
                snapshot = LibraryScanner(directory.parent).scan(changed_directories={directory})
                meeting = next((m for m in snapshot.meetings if m.directory == directory), None)
                if meeting is None or meeting.lifecycle != "saved":
                    raise ValueError("Meeting is not ready for Trash")
                if trash is None:

                    def trash(path):
                        return Gio.File.new_for_path(str(path)).trash(None)

                if cleanup:
                    cleanup.add(directory)  # Durable before the non-restorable portal call.
                try:
                    if not trash(directory):
                        raise OSError("Native Trash did not confirm success")
                except BaseException:
                    if cleanup:
                        with contextlib.suppress(OSError, ValueError):
                            cleanup.forget(directory)
                    raise
        if cleanup:
            try:
                cleanup.finish(directory)
            except (OSError, ValueError):
                # Trash succeeded. Keep the row removed and retry this private
                # bookkeeping from the durable intent, rather than claiming it failed.
                LOG.exception("Meeting is in Trash; private cleanup remains queued")
        else:
            location.forget_meeting(directory)
            if voices:
                voices.remove_meeting(directory)
        return directory


@dataclass
class PendingTrash:
    meeting: object
    window: object
    toast: object = None
    phase: str = "pending"
    waits: tuple = ()


class MeetingTrash:
    def __init__(self, app):
        self.app = app
        self.operations = {}
        self.hidden = {}  # Path -> deletion time, or None during the Undo window/job.
        self.absent = set()
        self.errors = {}
        self.toast = None
        self.waiters = []
        self.failed_flush = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-trash")
        self.cleanup = TrashCleanup(app.voice_store)
        self.executor.submit(self.cleanup.retry)

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
                # A later scan saw a system-Trash restore. Keep its metadata intact.
                self.hidden.pop(directory)
                self.absent.discard(directory)

    def request(self, meeting, window):
        if not self.allowed(meeting, window):
            return
        if self.toast:
            self.toast.dismiss()  # Replacing a toast commits its previous meeting.
        directory = meeting.directory.absolute()
        self.errors.pop(directory, None)
        operation = PendingTrash(meeting, window)
        self.operations[directory] = operation
        self.hidden[directory] = None
        operation.waits = self._prepare(meeting)
        toast = operation.toast = Adw.Toast(
            title=GLib.markup_escape_text(text("meeting.trashedToast", title=meeting.title)),
            button_label=text("common.undo"),
        )
        toast.connect("button-clicked", lambda *_: self.undo(operation))
        toast.connect("dismissed", lambda *_: self.commit(operation))
        self.toast = toast
        self._refresh()
        window.toasts.add_toast(toast)

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

    def window_hidden(self, window):
        for operation in list(self.operations.values()):
            if operation.window is window and operation.phase == "pending":
                operation.toast.dismiss()

    def undo(self, operation):
        if operation.phase != "pending":
            return
        operation.phase = "undone"
        directory = operation.meeting.directory.absolute()
        self.operations.pop(directory, None)
        self.hidden.pop(directory, None)
        operation.window._pending_selection = operation.meeting.key
        if self.toast is operation.toast:
            self.toast = None
        self._refresh()

    def commit(self, operation):
        if operation.phase != "pending":
            return
        operation.phase = "committing"
        if self.toast is operation.toast:
            self.toast = None
        self.app.hold()
        queue = self.app.transcriber

        def work():
            for future in operation.waits:
                with contextlib.suppress(Exception):
                    future.result()  # Drain finished/failed writers; their own UI reports the error.
            return trash_owned(operation.meeting.directory, queue=queue, cleanup=self.cleanup)

        future = self.executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(self._complete, operation, f))

    def _complete(self, operation, future):
        directory = operation.meeting.directory.absolute()
        try:
            future.result()
        except Exception as error:
            LOG.warning("Could not move meeting to Trash: %s", error)
            operation.phase = "failed"
            self.failed_flush = True
            self.hidden.pop(directory, None)
            unsupported = isinstance(error, GLib.Error) and error.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED
            )
            self.errors[directory] = "unsupported" if unsupported else "failed"
            operation.window._pending_selection = operation.meeting.key
            operation.window.present()
        else:
            operation.phase = "done"
            self.hidden[directory] = time.monotonic()
            self.app.discard_meeting_paths(directory)
        self.operations.pop(directory, None)
        self._refresh()
        for window in self.app.get_windows():
            if hasattr(window, "monitor"):
                window.monitor.refresh_now()
        if not self.operations:
            callbacks, self.waiters = self.waiters, []
            failed, self.failed_flush = self.failed_flush, False
            if not failed:
                for callback in callbacks:
                    callback()
        self.app.release()
        return GLib.SOURCE_REMOVE

    def flush(self, callback):
        """Commit on close/quit; keep GTK alive until the native Trash reply."""
        for operation in list(self.operations.values()):
            if operation.phase == "pending":
                operation.toast.dismiss()
        if not self.operations:
            return False
        if callback not in self.waiters:
            self.waiters.append(callback)
        return True

    def _refresh(self):
        for window in self.app.get_windows():
            snapshot = getattr(window, "_source_snapshot", None)
            if snapshot is not None:
                window._updated(snapshot)
