# SPDX-License-Identifier: GPL-3.0-or-later
"""Real-widget binding checks only; the Board exercises actual worker delivery."""

from types import SimpleNamespace

from gi.repository import Gtk

from cassini_talk.batch_import import Result
from cassini_talk.i18n import using_language
from cassini_talk.import_dialog import ImportDialog
from cassini_talk.strings import text


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
    print("PASS: D96 real progress, result and cancellation widget bindings")
