# SPDX-License-Identifier: GPL-3.0-or-later
"""Real-widget binding checks only; the Board exercises actual worker delivery."""

from types import SimpleNamespace

from gi.repository import Gtk

from cassini_talk.batch_import import Result
from cassini_talk.i18n import using_language
from cassini_talk.import_dialog import ImportDialog
from cassini_talk.library import LibrarySnapshot
from cassini_talk.strings import plural, text
from cassini_talk.zip_export_dialog import ZipExportDialog


class HeldFuture:
    def add_done_callback(self, callback):
        self.callback = callback

    def result(self):
        return self.value


class HeldExecutor:
    def submit(self, function, *args):
        self.submitted = function, args
        self.future = HeldFuture()
        return self.future


def shell():
    messages, refreshes = [], []
    return SimpleNamespace(
        monitor=SimpleNamespace(_closed=False, refresh_now=lambda: refreshes.append(True)),
        _opening_files=0,
        _import_dialogs=set(),
        _import_executor=HeldExecutor(),
        meeting_page=None,
        recording_page=None,
        search_page=None,
        toast=messages.append,
        messages=messages,
        refreshes=refreshes,
    )


def check_import_bindings():
    window = shell()
    with using_language("it"):
        dialog = ImportDialog(window, ("/fixture/Notes.zip",))
        assert window._opening_files == 1 and dialog in window._import_dialogs
        assert isinstance(dialog.cancel_button, Gtk.Button)
        assert dialog.cancel_button.get_label() == text("common.cancel")
        dialog._progress(Result(total=3, imported=1))
        assert dialog.status.get_label() == text("import.progress", done=2, total=3)
        assert dialog.get_title() == dialog.status.get_label()
        future = window._import_executor.future
        future.value = Result(total=3, imported=1, duplicates=1, ignored=1, last_key="Received-fixture")
        dialog._done(future)
        assert dialog.closed and window._opening_files == 0 and not window._import_dialogs
        assert window._pending_selection == "Received-fixture" and window.refreshes == [True]
        assert window.messages == ["1 importata · 1 duplicato saltato · 1 file non supportato ignorato"]
    window = shell()
    dialog = ImportDialog(window, ("/fixture/Cancel.zip",))
    dialog.cancel_button.emit("clicked")
    assert dialog.cancelled.is_set() and dialog.closed

    # Queued dialog does not present until started; cancelling before start suppresses presentation;
    # unexpected worker exceptions retain partial counts and report the failure.
    window = shell()
    presented = []
    queued = ImportDialog(window, ("/fixture/Queued.zip",))
    queued.present = lambda parent: presented.append(parent)
    assert not presented
    queued.cancel()
    queued._show()
    assert not presented

    window = shell()
    with using_language("en"):
        crashing = ImportDialog(window, ("/fixture/Crash.zip",))
        crashing._progress(Result(total=3, imported=2, last_key="Received-two"))
        boom_future = window._import_executor.future
        boom_future.result = lambda: (_ for _ in ()).throw(RuntimeError("Synthetic worker crash"))
        crashing._done(boom_future)
        assert window._pending_selection == "Received-two" and window.refreshes == [True]
        assert window.messages == ["Imported 2 · 1 file could not be imported"]

    # Localized ZipExportDialog bindings: all/selected visibility, progress and plural completion toast.
    from pathlib import Path

    fake_meetings = (
        SimpleNamespace(
            key="Received-1",
            title="Standup",
            lifecycle="saved",
            has_audio=True,
            published_missing=False,
            directory=Path("/tmp/Received-1"),
        ),
        SimpleNamespace(
            key="Received-2",
            title="Retro",
            lifecycle="saved",
            has_audio=True,
            published_missing=False,
            directory=Path("/tmp/Received-2"),
        ),
    )
    added_toasts = []
    export_window = SimpleNamespace(
        snapshot=LibrarySnapshot(meetings=fake_meetings),
        tz=None,
        toasts=SimpleNamespace(add_toast=added_toasts.append),
        toast=lambda msg: added_toasts.append(msg),
        _refresh_export=lambda: None,
        get_application=lambda: SimpleNamespace(
            recording_settings=SimpleNamespace(get_string=lambda _k: "", set_string=lambda _k, _v: None),
            meeting_trash=SimpleNamespace(hides=lambda _d: False),
        ),
    )
    with using_language("it"):
        zip_dialog = ZipExportDialog(export_window)
        assert zip_dialog.get_title() == text("export.zip.all")
        assert zip_dialog.export_all_button.get_label() == text("export.zip.all")
        assert zip_dialog.export_selected_button.get_label() == text("export.zip.selected")
        assert zip_dialog.export_selected_button.get_visible()
        zip_dialog.checks["Received-1"].set_active(False)
        zip_dialog.checks["Received-2"].set_active(False)
        assert not zip_dialog.export_selected_button.get_visible()
        zip_dialog.checks["Received-2"].set_active(True)
        assert zip_dialog.export_selected_button.get_visible()
        zip_dialog._progress(2, 2)
        assert zip_dialog.progress_label.get_label() == text("export.zip.progress", done=2, total=2)
        zip_dialog._target_count = 2
        done_future = HeldFuture()
        done_future.value = Path("/tmp/Cassini.zip")
        zip_dialog._done(done_future)
        assert added_toasts[-1].get_title() == plural("export.zip.done", 2)
        assert added_toasts[-1].get_button_label() == text("common.show")
        zip_dialog._closed()
    print("PASS: D96 real progress, result, queued-cancel, ZIP export and exception widget bindings")
