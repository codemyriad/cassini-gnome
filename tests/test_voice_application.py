# SPDX-License-Identifier: GPL-3.0-or-later
"""Application voice bookkeeping without initialising GTK or connecting devices."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_speakers import meeting  # noqa: F401
from test_voice_store import RECORDING, VoiceStore, enrol

from cassini_viewer.library import LibrarySnapshot


@pytest.fixture(scope="module")
def application_class(tmp_path_factory):
    gi = pytest.importorskip("gi")
    try:
        for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
            gi.require_version(namespace, version)
    except ValueError:
        pytest.skip("Application bookkeeping also runs in GTK-enabled distro and Recording jobs")
    from gi.repository import Gio

    compiler = shutil.which("glib-compile-resources")
    if compiler is None:
        pytest.skip("The application resource compiler is unavailable")
    source = Path(__file__).parents[1] / "src"
    resource = tmp_path_factory.mktemp("voice-resources") / "cassini.gresource"
    subprocess.run(
        [
            compiler,
            str(source / "cassini-viewer.gresource.xml"),
            "--sourcedir=" + str(source),
            "--target=" + str(resource),
        ],
        check=True,
    )
    Gio.resources_register(Gio.Resource.load(str(resource)))
    from cassini_viewer.main import CassiniApplication

    return CassiniApplication


def test_unavailable_library_preserves_voice_meeting_lists_until_a_valid_scan(application_class, tmp_path):
    store = VoiceStore(tmp_path / "private")
    alice = enrol(store)
    store.sync_library([RECORDING])
    writes = []

    def submit(operation, keys):
        writes.append(keys)
        operation(keys)

    monitor = SimpleNamespace(_closed=False, _executor=SimpleNamespace(submit=submit))
    app = SimpleNamespace(
        _voice_library_keys=frozenset([RECORDING]),
        get_windows=lambda: [SimpleNamespace(monitor=monitor)],
        voice_store=store,
        settings_dialog=None,
        transcription_enabled=False,
    )
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    application_class.observe_library(app, LibrarySnapshot(unavailable=True), None)
    assert not writes and app._voice_library_keys == frozenset([RECORDING])
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before
    assert store.snapshot()["voices"] == [alice]
    available = LibrarySnapshot(meetings=(SimpleNamespace(directory=Path(RECORDING)),))
    application_class.observe_library(app, available, None)
    assert not writes and store.snapshot()["voices"] == [alice]
    # A successful empty scan really does mean the meeting left the Library.
    application_class.observe_library(app, LibrarySnapshot(), None)
    assert writes == [frozenset()] and store.snapshot()["voices"][0]["meetings"] == []


def test_windowless_observation_retries_voice_sync_when_a_monitor_becomes_available(application_class, tmp_path):
    store = VoiceStore(tmp_path / "private")
    enrol(store)
    windows, calls = [], []
    app = SimpleNamespace(
        _voice_library_keys=None,
        get_windows=lambda: windows,
        voice_store=store,
        settings_dialog=None,
        transcription_enabled=False,
    )
    snapshot = LibrarySnapshot(meetings=(SimpleNamespace(directory=Path(RECORDING)),))
    application_class.observe_library(app, snapshot, None)
    assert app._voice_library_keys is None

    def submit(operation, keys):
        calls.append(keys)
        operation(keys)

    windows.append(SimpleNamespace(monitor=SimpleNamespace(_closed=False, _executor=SimpleNamespace(submit=submit))))
    application_class.observe_library(app, snapshot, None)
    assert calls == [frozenset([RECORDING])]
    assert app._voice_library_keys == frozenset([RECORDING])


def test_recompute_holds_follow_atomic_queue_acceptance(application_class):
    holds = []
    started = iter([True, False, True])
    app = SimpleNamespace(
        _quitting=False,
        _capture_exiting=False,
        _transcription_holds=0,
        meeting_trash=SimpleNamespace(hides=lambda _: False),
        _ensure_transcriber=lambda: SimpleNamespace(recompute_voices=lambda *_: next(started)),
        hold=lambda: holds.append(True),
    )
    for expected in (1, 1, 2):
        application_class.recompute_voices(app, Path(RECORDING), {"a"})
        assert app._transcription_holds == len(holds) == expected


@pytest.mark.parametrize("navigated_back", [False, True])
def test_undo_recomputes_the_actual_popped_pair_even_after_navigating_back(application_class, meeting, navigated_back):  # noqa: F811
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from test_library_monitor import spin_until

    from cassini_viewer.meeting_page import MeetingPage
    from cassini_viewer.speaker_edits import current_meeting, merge_into

    directory = meeting.directory
    merge_into(directory, "a", "b")
    stale = current_meeting(directory)
    calls, refreshed = [], []
    app = SimpleNamespace(recompute_voices=lambda path, affected: calls.append((path, tuple(affected))))
    release = threading.Event()
    with ThreadPoolExecutor(1) as executor:
        blocker = executor.submit(release.wait, 4)
        monitor = SimpleNamespace(
            _closed=False, _executor=executor, scanner=None, refresh_now=lambda: refreshed.append(True)
        )
        window = SimpleNamespace(
            monitor=monitor,
            get_application=lambda: app,
            lookup_action=lambda _: SimpleNamespace(set_enabled=lambda _: None),
        )
        page = SimpleNamespace(_closed=False, _speaker_future=None, meeting=stale, window=window)
        page._merge_undone = lambda future: MeetingPage._merge_undone(page, future)
        try:
            MeetingPage.undo_speaker_merge(page)
            # A newer merge lands before the queued Undo; the UI remains stale.
            merge_into(directory, "b", "c")
            page._closed = navigated_back
            release.set()
            blocker.result(timeout=4)
            spin_until(lambda: page._speaker_future is None)
            assert calls == [(directory, ("b", "c"))] and refreshed == [True]
            assert current_meeting(directory).metadata["speakerMerges"] == [{"from": "a", "into": "b"}]
        finally:
            release.set()


def test_removed_meeting_paths_cannot_be_resaved_by_hidden_callbacks(application_class, tmp_path):
    gone, kept = tmp_path / RECORDING, tmp_path / ("Received-" + RECORDING.removeprefix("Recording-"))
    gone_path, kept_path = str(gone), str(kept)
    removed = SimpleNamespace(session=SimpleNamespace(directory=gone), pipeline=None)
    retained = SimpleNamespace(session=SimpleNamespace(directory=kept), pipeline=None)
    discarded = []
    window = SimpleNamespace(
        _pending_recording=gone_path, _browser_snapshot=dict(recordings=[dict(path=gone_path), dict(path=kept_path)])
    )
    window._activity_source = SimpleNamespace(
        paths={"gone": gone_path, "kept": kept_path}, snapshot={"gone": "recording", "kept": "recording"}
    )
    app = SimpleNamespace(
        _pending_recording=gone_path,
        _capture_paths={gone_path, kept_path},
        _browser_seen_saved={gone_path, kept_path},
        _browser_snapshot=dict(
            recordings=[dict(path=gone_path), dict(path=kept_path)],
            statuses=[dict(path=gone_path), dict(path=kept_path)],
        ),
        native_captures={"gone": removed, "kept": retained},
        _last_native=removed,
        comfort=SimpleNamespace(discard_meeting=discarded.append),
        get_windows=lambda: [window],
        meeting_trash=SimpleNamespace(hides=lambda path: Path(path) == gone),
        _quitting=False,
        _capture_exiting=False,
    )
    application_class.discard_meeting_paths(app, gone)
    assert app._pending_recording is None and window._pending_recording is None
    assert app._browser_seen_saved == {kept_path} and app._last_native is None
    assert app._capture_paths == {kept_path}
    assert window._activity_source.paths == {"kept": kept_path}
    assert window._activity_source.snapshot == {"kept": "recording"}
    assert app.native_captures == {"kept": retained} and discarded == [gone]
    assert app._browser_snapshot == dict(recordings=[dict(path=kept_path)], statuses=[dict(path=kept_path)])
    assert window._browser_snapshot["recordings"] == [dict(path=kept_path)]
    # No queue/model creation, hold or write is attempted for the removed path.
    application_class.write_down(app, gone)
    application_class.recompute_voices(app, gone, {"a"})
    # Even a stale browser poll cannot put the removed path back in hidden saves.
    from cassini_viewer.capture_guard import remember_capture_paths

    window._activity_source.paths["stale"] = gone_path
    assert remember_capture_paths(app) == {kept_path}
