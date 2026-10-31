# SPDX-License-Identifier: GPL-3.0-or-later
"""Application windowless bookkeeping without initialising GTK or connecting devices."""

from types import SimpleNamespace

import pytest
from test_voice_application import application_class  # noqa: F401


def closing_app(monkeypatch, *, held=True, quitting=False):
    from cassini_talk import main

    idle = []
    monkeypatch.setattr(main.GLib, "idle_add", lambda function, *args: idle.append((function, args)) or 1)
    refreshes, exits = [], []
    window = SimpleNamespace()
    app = SimpleNamespace(
        _closing_background_windows=False,
        _auto_recording_held=held,
        _quitting=quitting,
        _capture_background_active=lambda: False,
        get_windows=lambda: [window],
        begin_exit=lambda: exits.append(True),
        windowless_background=SimpleNamespace(refresh=lambda: refreshes.append(True)),
    )
    return app, window, idle, refreshes, exits


def test_closing_the_last_window_while_armed_refreshes_windowless_state_on_idle(application_class, monkeypatch):  # noqa: F811
    app, window, idle, refreshes, exits = closing_app(monkeypatch)
    application_class.window_closing(app, window)
    assert not exits and not refreshes, "GTK has not hidden the window yet"
    assert len(idle) == 1
    function, args = idle.pop()
    assert function(*args) is False and refreshes == [True]


@pytest.mark.parametrize("held, quitting", [(False, False), (True, True)])
def test_exiting_or_quitting_close_schedules_no_windowless_refresh(application_class, monkeypatch, held, quitting):  # noqa: F811
    app, window, idle, refreshes, exits = closing_app(monkeypatch, held=held, quitting=quitting)
    application_class.window_closing(app, window)
    assert not idle and not refreshes
    assert exits == ([True] if not held else [])
