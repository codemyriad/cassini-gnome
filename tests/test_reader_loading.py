# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader projection stays off GTK and stale transcript requests cannot win."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_voice_application import application_class  # noqa: F401


@pytest.fixture
def reader_class(application_class):  # noqa: F811
    from cassini_talk.window import CassiniWindow

    return CassiniWindow


@pytest.mark.parametrize("initial", [False, True])
def test_projection_runs_on_a_worker_and_is_only_applied_on_delivery(reader_class, monkeypatch, initial):
    from cassini_talk import window

    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
    main = threading.get_ident()
    meeting = SimpleNamespace(is_readable=True, default_transcript_id="selected")
    rows = [object()]
    callbacks, workers = [], []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def load(current, identifier):
        assert threading.get_ident() != main
        assert current is meeting and identifier == "selected"
        entered.set()
        assert release.wait(20)
        return rows, None

    def idle(callback, *args):
        callbacks.append((callback, args))
        delivered.set()
        return 1

    fake = SimpleNamespace(
        _generation=1,
        meeting=meeting,
        transcript_id="old",
        _chooser_ids=["selected"],
        _on_loaded=Mock(),
        _set_turns=Mock(),
        _populate_properties=Mock(),
        _update_subtitle=Mock(),
        _cancel_verification=Mock(),
        _cancel_transcript_scroll=Mock(),
        _exact_transcript=Mock(),
        main_stack=Mock(),
        set_title=Mock(),
        toast=Mock(),
    )
    fake._on_transcript_loaded = lambda *args: reader_class._on_transcript_loaded(fake, *args)
    monkeypatch.setattr(window, "load_reader_turns", load)
    monkeypatch.setattr(window.portable, "read_meeting", lambda _path: meeting)
    monkeypatch.setattr(window.threading, "Thread", thread)
    monkeypatch.setattr(window.GLib, "idle_add", idle)
    try:
        if initial:
            file = SimpleNamespace(get_path=lambda: "/reader.opus", get_basename=lambda: "reader.opus")
            reader_class.open_file(fake, file)
        else:
            reader_class._on_transcript_selected(fake, SimpleNamespace(get_selected=lambda: 0), None)
        assert entered.wait(20)
        fake._set_turns.assert_not_called()
        fake._on_loaded.assert_not_called()
        release.set()
        assert delivered.wait(20)
        callback, args = callbacks.pop()
        callback(*args)
        if initial:
            fake._on_loaded.assert_called_once_with(2, file, meeting, rows, None)
            fake._cancel_transcript_scroll.assert_called_once_with()
            fake._exact_transcript.reset_turns.assert_called_once_with(())
        else:
            fake._set_turns.assert_called_once_with(rows, None)
    finally:
        release.set()
        for worker in workers:
            worker.join(20)
            assert not worker.is_alive()


@pytest.mark.parametrize("stale", ["file", "request", "meeting"])
def test_stale_projection_never_overwrites_the_current_reader(reader_class, stale):
    meeting, request = object(), object()
    fake = SimpleNamespace(
        _generation=2,
        _transcript_request=request,
        meeting=meeting,
        _set_turns=Mock(),
        _populate_properties=Mock(),
        _update_subtitle=Mock(),
    )
    generation = 1 if stale == "file" else 2
    assert (
        reader_class._on_transcript_loaded(
            fake,
            generation,
            object() if stale == "request" else request,
            object() if stale == "meeting" else meeting,
            [object()],
            None,
        )
        is False
    )
    fake._set_turns.assert_not_called()
    fake._populate_properties.assert_not_called()
    fake._update_subtitle.assert_not_called()


def test_closing_the_reader_invalidates_pending_models(reader_class):
    app = SimpleNamespace(confirm_capture_close=lambda _window: False, window_closing=Mock())
    touch = Mock()
    fake = SimpleNamespace(
        get_application=lambda: app,
        _generation=3,
        _transcript_request=object(),
        get_default_size=lambda: (800, 600),
        is_maximized=lambda: False,
        settings=Mock(),
        _cancel_verification=Mock(),
        _cancel_transcript_scroll=Mock(),
        _exact_transcript=Mock(),
        _follow_signal=11,
        _transcript_style_signals=[12, 13],
        style_manager=Mock(),
        _touch_scroll_signals=[(touch, 14)],
        _uninhibit=Mock(),
        player=Mock(),
    )
    meeting, request = object(), fake._transcript_request
    fake.meeting, fake._set_turns = meeting, Mock()
    fake._populate_properties, fake._update_subtitle = Mock(), Mock()
    assert reader_class._on_close_request(fake) is False
    fake._cancel_transcript_scroll.assert_called_once_with()
    fake._exact_transcript.close.assert_called_once_with()
    fake.settings.disconnect.assert_called_once_with(11)
    fake.style_manager.disconnect.assert_any_call(12)
    fake.style_manager.disconnect.assert_any_call(13)
    touch.disconnect.assert_called_once_with(14)
    assert not fake._transcript_style_signals and not fake._touch_scroll_signals
    reader_class._on_transcript_loaded(fake, 3, request, meeting, [object()], None)
    fake._set_turns.assert_not_called()


def test_reader_word_failure_preserves_the_error_without_building_rows(monkeypatch):
    from cassini_talk import portable
    from cassini_talk.meeting_text import load_reader_turns

    def broken(*_):
        raise portable.CassiniError("damaged words")

    monkeypatch.setattr(portable, "load_words", broken)
    assert load_reader_turns(SimpleNamespace(manifest={}), "broken") == ([], "damaged words")


def test_populating_the_chooser_does_not_start_redundant_transcript_loads(reader_class, monkeypatch):
    from cassini_talk import window

    load = Mock(side_effect=AssertionError("Programmatic selection started a load"))
    monkeypatch.setattr(window, "load_reader_turns", load)
    dropdown = SimpleNamespace(get_selected=Mock(side_effect=AssertionError("Selection should be ignored")))
    fake = SimpleNamespace(
        meeting=SimpleNamespace(
            is_readable=True,
            transcripts=[SimpleNamespace(id="first", language=None), SimpleNamespace(id="selected", language=None)],
        ),
        transcript_id="selected",
        transcript_chooser=Mock(),
    )

    def notify(*_):
        return reader_class._on_transcript_selected(fake, dropdown, None)

    fake.transcript_dropdown = SimpleNamespace(
        set_model=Mock(side_effect=notify), set_selected=Mock(side_effect=notify)
    )
    reader_class._populate_transcript_chooser(fake)
    assert not fake._updating_transcript_chooser
    fake.transcript_dropdown.set_selected.assert_called_once_with(1)
    load.assert_not_called()
