# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1140: the libadwaita 1.5 (Ubuntu 24.04) AdwDialog close workaround, with fakes (no display)."""

import pytest


class FakeDialog:
    """Emits "closed" on the *closes_on*-th force_close (or close when *close_works*)."""

    def __init__(self, closes_on=1, close_works=False, accepts=True):
        self.handlers, self.next_id, self.parent = {}, 0, object()
        self.force_calls = self.close_calls = 0
        self.closes_on, self.close_works, self.accepts = closes_on, close_works, accepts

    def connect(self, signal, callback):
        assert signal == "closed"
        self.next_id += 1
        self.handlers[self.next_id] = callback
        return self.next_id

    def disconnect(self, handler):
        del self.handlers[handler]

    def get_parent(self):
        return self.parent

    def emit_closed(self):
        for callback in list(self.handlers.values()):
            callback(self)
        self.parent = None


@pytest.fixture
def compat(monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib

    from cassini_talk import adw_compat

    def original_force_close(dialog):
        dialog.force_calls += 1
        if dialog.force_calls >= dialog.closes_on:
            dialog.emit_closed()

    def original_close(dialog):
        dialog.close_calls += 1
        if dialog.accepts and dialog.close_works:
            dialog.emit_closed()
        return dialog.accepts

    timers = []
    monkeypatch.setattr(Adw.Dialog, "close", original_close)
    monkeypatch.setattr(Adw.Dialog, "force_close", original_force_close)
    monkeypatch.setattr(GLib, "timeout_add", lambda ms, callback: timers.append((ms, callback)) or len(timers))
    return Adw, adw_compat, timers


def run(timers):
    """Run the timers like a main loop until each returns SOURCE_REMOVE."""
    while timers:
        ms, callback = timers.pop(0)
        if callback():
            timers.append((ms, callback))


def test_nothing_is_patched_from_libadwaita_1_8(compat):
    Adw, adw_compat, _timers = compat
    before = Adw.Dialog.close, Adw.Dialog.force_close
    assert not adw_compat.install_dialog_close_fix((1, 8))
    assert not adw_compat.install_dialog_close_fix((1, 9))
    assert (Adw.Dialog.close, Adw.Dialog.force_close) == before


def test_force_close_in_the_first_frames_is_retried_until_closed(compat):
    Adw, adw_compat, timers = compat
    assert adw_compat.install_dialog_close_fix((1, 5))
    assert not adw_compat.install_dialog_close_fix((1, 5)), "installs once"
    dialog = FakeDialog(closes_on=3)
    Adw.Dialog.force_close(dialog)
    assert dialog.force_calls == 1 and timers[0][0] == adw_compat.FORCE_CLOSE_RETRY_MS
    run(timers)
    assert dialog.force_calls == 3 and dialog.parent is None and not dialog.handlers


def test_a_dialog_that_closes_at_once_leaves_no_timer_or_handler(compat):
    Adw, adw_compat, timers = compat
    adw_compat.install_dialog_close_fix((1, 5))
    dialog = FakeDialog(closes_on=1)
    Adw.Dialog.force_close(dialog)
    assert not timers and not dialog.handlers
    dialog = FakeDialog(close_works=True)
    assert Adw.Dialog.close(dialog) is True
    assert not timers and not dialog.handlers and dialog.force_calls == 0


def test_an_accepted_close_that_never_finishes_is_forced_after_the_grace(compat):
    Adw, adw_compat, timers = compat
    adw_compat.install_dialog_close_fix((1, 5))
    dialog = FakeDialog(closes_on=2)
    assert Adw.Dialog.close(dialog) is True
    assert [ms for ms, _ in timers] == [adw_compat.CLOSE_GRACE_MS] and dialog.force_calls == 0
    run(timers)
    assert dialog.force_calls == 2 and dialog.parent is None and not dialog.handlers


def test_a_refused_close_is_left_to_the_dialog(compat):
    Adw, adw_compat, timers = compat
    adw_compat.install_dialog_close_fix((1, 5))
    dialog = FakeDialog(accepts=False)
    assert Adw.Dialog.close(dialog) is False
    assert not timers and not dialog.handlers and dialog.force_calls == 0


def test_retries_stop_when_the_dialog_leaves_its_window(compat):
    Adw, adw_compat, timers = compat
    adw_compat.install_dialog_close_fix((1, 5))
    dialog = FakeDialog(closes_on=99)
    Adw.Dialog.force_close(dialog)
    dialog.parent = None  # Its window was destroyed.
    run(timers)
    assert dialog.force_calls == 1 and not dialog.handlers
