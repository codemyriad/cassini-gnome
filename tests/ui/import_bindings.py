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
    print("PASS: D96 real progress, result, queued-cancel and exception widget bindings")
