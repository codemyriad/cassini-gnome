# SPDX-License-Identifier: GPL-3.0-or-later
"""D15.1 ownership, durable deferral, producer routing and recovery, on private roots."""

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassini_viewer import library_location as location
from cassini_viewer.library import LibraryCollectionScanner, library_root
from cassini_viewer.meeting_store import update_metadata
from cassini_viewer.recording_details import edit_recording
from cassini_viewer.recording_session import BrowserHost, RecordingSession


@pytest.fixture
def roots(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(old))
    return old, new


def session(root, title="A meeting"):
    recording = RecordingSession(root, title)
    recording.add_track("mic", "Me", "microphone", 48000, 1)
    recording.append("mic", b"\x01\x00" * 4800, 0)
    return recording


def test_destination_publishes_immediately_and_preexisting_host_uses_it(roots):
    old, new = roots
    recording = session(old)
    recording.finish()
    browser = BrowserHost()
    location.request(new)
    assert library_root() == new and recording.directory.exists()
    path = browser.handle(dict(type="start"))["path"]
    assert browser.session.directory.parent == new and str(new) in path
    browser.handle(dict(type="stop"))
    location.process()
    assert not recording.directory.exists() and (new / recording.directory.name).is_dir()


def test_live_capture_and_notes_stay_at_old_path_until_stop_then_move(roots):
    old, new = roots
    recording = session(old)
    location.request(new)
    location.process(activity={recording.manifest["id"]: "recording"})
    assert len(location.load()["pending"]) == 1
    assert location.roots() == (new, old)
    edit_recording(recording.directory, notes=[dict(atMs=0, text="While still recording")])
    recording.append("mic", b"\x02\x00" * 4800, 100)
    recording.finish()
    expected = {p.name: p.read_bytes() for p in recording.directory.iterdir()}
    result = location.process()
    assert result["pending"] == [] and location.roots() == (new,)
    actual = new / recording.directory.name
    assert {p.name: p.read_bytes() for p in actual.iterdir()} == expected
    # An already queued editor with the old path follows the journaled move.
    edit_recording(recording.directory, title="Last final edit")
    assert location.owned_directory(recording.directory) == actual


def test_transcribing_and_saving_are_durable_busy_states(roots):
    old, new = roots
    writing, saving = session(old, "Writing"), session(old, "Saving")
    writing.finish()
    update_metadata(writing.directory, transcript=dict(status="running", reference=None))
    location.request(new)
    result = location.process(activity={saving.manifest["id"]: "saving"})
    assert len(result["pending"]) == 2
    # A fresh scanner after restart sees both retained folders as ordinary rows.
    scanner = LibraryCollectionScanner()
    assert {m.directory for m in scanner.scan().meetings} == {writing.directory, saving.directory}
    update_metadata(writing.directory, transcript=dict(status="none", reference=None))
    saving.finish()
    assert location.process()["pending"] == []
    assert {m.directory.parent for m in scanner.scan().meetings} == {new}


def test_collision_keeps_both_meetings_and_retry_only_retries_failed(roots):
    old, new = roots
    recording, waiting = session(old), session(old, "Waiting")
    recording.finish()
    waiting.finish()
    new.mkdir()
    shutil.copytree(recording.directory, new / recording.directory.name)
    location.request(new)
    result = location.process(busy={waiting.directory})
    assert {e["state"] for e in result["pending"]} == {"failed", "waiting"}
    rows = LibraryCollectionScanner().scan().meetings
    assert len(rows) == 3 and len({m.key for m in rows}) == 3
    assert recording.directory.is_dir()
    shutil.rmtree(new / recording.directory.name)
    result = location.process(retry=True)
    assert len(result["pending"]) == 1 and waiting.directory.exists()
    assert result["pending"][0]["source"] == str(waiting.directory)
    assert location.process()["pending"] == []


def test_unrelated_old_folder_is_not_owned_or_adopted_by_a_later_move(roots, tmp_path):
    old, new = roots
    recording = session(old)
    recording.finish()
    location.request(new)
    other = session(old, "Not in this Library")
    other.finish()
    with pytest.raises(ValueError, match="outside"):
        location.owned_directory(other.directory)
    location.request(tmp_path / "third")
    location.process()
    assert other.directory.exists()
    assert not any(m.directory == other.directory for m in LibraryCollectionScanner().scan().meetings)


def test_publication_before_journal_checkpoint_recovers_on_restart(roots):
    old, new = roots
    recording = session(old)
    recording.finish()
    location.request(new)
    recording.directory.rename(new / recording.directory.name)
    assert location.process()["pending"] == []
    assert location.owned_directory(recording.directory) == new / recording.directory.name


def test_new_recording_does_not_wait_for_old_meeting_copy(roots, monkeypatch):
    from cassini_viewer import library_move

    old, new = roots
    recording = session(old)
    recording.finish()
    location.request(new)
    entered, proceed = threading.Event(), threading.Event()
    original = library_move.move_folder

    def blocked(source, target, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original(source, target, **kwargs)

    monkeypatch.setattr(library_move, "move_folder", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        moving = pool.submit(location.process)
        assert entered.wait(2)
        browser = BrowserHost()
        started = pool.submit(browser.handle, dict(type="start"))
        try:
            started.result(timeout=2)
            assert browser.session.directory.parent == new
        finally:
            proceed.set()
        moving.result(timeout=5)
        browser.handle(dict(type="stop"))


def test_cancelled_move_keeps_durable_pending_for_next_launch(roots):
    old, new = roots
    recording = session(old)
    recording.finish()
    location.request(new)
    event = threading.Event()
    event.set()
    assert len(location.process(cancelled=event)["pending"]) == 1
    assert recording.directory.is_dir()
    assert location.process()["pending"] == []


def test_two_capture_leases_are_independent_and_stop_moves_only_the_idle_one(roots):
    old, new = roots
    first, second = BrowserHost(), BrowserHost()
    first.handle(dict(type="start"))
    second.handle(dict(type="start"))
    one, two = first.session.directory, second.session.directory
    location.request(new)
    assert len(location.process()["pending"]) == 2
    first.handle(dict(type="stop"))
    assert len(location.process()["pending"]) == 1
    assert not one.exists() and two.exists()
    second.handle(dict(type="stop"))
    assert location.process()["pending"] == []
    assert (new / one.name).is_dir() and (new / two.name).is_dir()


def test_import_in_progress_publishes_into_new_primary_without_blocking_routing(roots, monkeypatch):
    from pathlib import Path

    from cassini_viewer import received

    old, new = roots
    original = Path(__file__).parents[1] / "ux/library-vectors/assets/plain-title.opus"
    before_publish, proceed = threading.Event(), threading.Event()
    publish = received._publish_import

    def blocked(stage, identifier, root):
        before_publish.set()
        assert proceed.wait(5)
        return publish(stage, identifier, root)

    monkeypatch.setattr(received, "_publish_import", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(received.import_received, original)
        assert before_publish.wait(3)
        switching = pool.submit(location.request, new)
        try:
            switching.result(timeout=2)
            assert library_root() == new
        finally:
            proceed.set()
        meeting = importing.result(timeout=5)
    assert meeting.directory.parent == new
    assert meeting.audio_file.read_bytes() == original.read_bytes()
    assert not list(old.glob(".received-*")) and not list(new.glob(".received-*"))


def test_progress_journal_recovers_even_if_final_compaction_is_interrupted(roots, monkeypatch):
    old, new = roots
    recording = session(old)
    recording.finish()
    location.request(new)
    compact = location.save

    def crash(value):
        if not value["pending"]:
            raise RuntimeError("Crash before final journal compaction")
        compact(value)

    monkeypatch.setattr(location, "save", crash)
    with pytest.raises(RuntimeError, match="Crash"):
        location.process()
    assert location.load()["pending"] == []
    assert location.owned_directory(recording.directory) == new / recording.directory.name
    # A partially appended final record is ignored; earlier complete records survive.
    with location.config_path().with_suffix(".journal").open("ab") as stream:
        stream.write(b'{"incomplete":')
    assert location.load()["pending"] == []


def test_move_checkpoints_write_linear_metadata_instead_of_rewriting_every_plan(roots, monkeypatch):
    import json

    old, new = roots
    for _ in range(30):
        recording = session(old)
        recording.finish()
    plan = location.request(new)
    budget = len(json.dumps(plan).encode())
    writes = []
    compact, append = location.save, location._append_progress

    def saved(value):
        writes.append(len(json.dumps(value).encode()))
        compact(value)

    def appended(value, entry, done):
        writes.append(len(json.dumps(entry).encode()) + 100)
        append(value, entry, done)

    monkeypatch.setattr(location, "save", saved)
    monkeypatch.setattr(location, "_append_progress", appended)
    assert location.process()["pending"] == []
    assert len(writes) == 31 and sum(writes) < 4 * budget


def test_actual_transcription_queue_keeps_old_paths_and_accepts_new_root_after_move(roots, tmp_path):
    from cassini_viewer.transcription import TranscriptionQueue

    old, new = roots
    recording = session(old)
    recording.finish()
    entered, release = threading.Event(), threading.Event()
    completed = threading.Event()
    paths = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            paths.append(tracks[0]["path"])
            entered.set()
            assert release.wait(5)
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, lambda _: completed.set(), client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(recording.directory)
        assert entered.wait(3)
        location.request(new)
        assert len(location.process()["pending"]) == 1
        assert recording.directory.is_dir() and str(old) in paths[0]
        release.set()
        assert completed.wait(3)
        assert location.process()["pending"] == []
        another = session(new)
        another.finish()
        entered.clear()
        completed.clear()
        assert queue.enqueue(another.directory)
        assert completed.wait(3)
        assert str(new) in paths[1]
    finally:
        release.set()
        queue.close()


def test_collection_preserves_disconnected_symlink_as_offline_not_empty(roots, tmp_path, monkeypatch):
    old, _new = roots
    recording = session(old)
    recording.finish()
    link = tmp_path / "Library-link"
    link.symlink_to(old, target_is_directory=True)
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(link))
    scanner = LibraryCollectionScanner()
    assert len(scanner.scan().meetings) == 1
    old.rename(tmp_path / "disconnected")
    assert scanner.scan().unavailable


def test_default_capture_and_late_edits_follow_canonical_move_from_a_symlink(roots, tmp_path, monkeypatch):
    old, new = roots
    old.mkdir()
    link = tmp_path / "Library-link"
    link.symlink_to(old, target_is_directory=True)
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(link))
    browser = BrowserHost()
    browser.handle(dict(type="start"))
    directory = browser.session.directory
    assert directory.parent == old
    browser.handle(dict(type="stop"))
    location.request(new)
    assert location.process()["pending"] == []
    edit_recording(link / directory.name, title="Edit queued at the old symlink")
    assert location.owned_directory(link / directory.name) == new / directory.name


def test_trash_discards_its_pending_move_and_aliases_only(roots):
    old, new = roots
    removed, kept = session(old), session(old)
    removed.finish()
    kept.finish()
    location.request(new)
    value = location.load()
    alias = str(old.parent / "earlier" / removed.directory.name)
    value["aliases"][alias] = str(removed.directory)
    location.save(value)
    location.forget_meeting(removed.directory)
    value = location.load()
    assert [e["source"] for e in value["pending"]] == [str(kept.directory)]
    assert alias not in value["aliases"]
    assert removed.directory.exists(), "routing cleanup must never delete meeting files"


def test_missing_removed_folder_is_not_a_failed_library_move(roots):
    old, new = roots
    removed = session(old)
    removed.finish()
    location.request(new)
    removed.directory.rename(old / "outside-library-private-trash")
    result = location.process()
    assert not result["pending"] and not (new / removed.directory.name).exists()


@pytest.mark.parametrize("mounted_music", [False, True])
def test_flatpak_default_uses_only_host_backed_storage(tmp_path, monkeypatch, mounted_music):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CASSINI_LIBRARY_DIR", raising=False)
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer.Devel")
    data = tmp_path / "private-app-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    music = tmp_path / "Musica" / "Cassini"
    monkeypatch.setattr(location, "music_directory", lambda: music.parent)
    monkeypatch.setattr(location, "_sandbox_mounts", lambda: (data, music) if mounted_music else (data,))
    expected = music if mounted_music else data / "cassini/Library"
    assert location.initial_root() == expected
    recording = RecordingSession(None, source="gnome", title="Persistent")
    try:
        assert recording.directory.parent == expected
    finally:
        recording.finish()


def test_flatpak_rejects_unmounted_private_data_instead_of_using_tmpfs(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSINI_LIBRARY_DIR", raising=False)
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer.Devel")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "unmounted-data"))
    monkeypatch.setattr(location, "_sandbox_mounts", lambda: ())
    with pytest.raises(OSError, match="persistent storage"):
        location.initial_root()


def test_flatpak_mount_checks_follow_symlinks_and_exact_path_boundaries(tmp_path, monkeypatch):
    mounted = tmp_path / "host mount"
    mounted.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(mounted, target_is_directory=True)
    monkeypatch.setattr(location, "_sandbox_mounts", lambda: (mounted,))
    assert location._host_storage(alias / "Library")
    assert not location._host_storage(tmp_path / "host mount-extra/Library")


def test_flatpak_fallback_selection_survives_music_becoming_available(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSINI_LIBRARY_DIR", raising=False)
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer.Devel")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    data = tmp_path / "app-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    music = tmp_path / "Music/Cassini"
    monkeypatch.setattr(location, "music_directory", lambda: music.parent)
    monkeypatch.setattr(location, "_sandbox_mounts", lambda: (data,))
    fallback = data / "cassini/Library"
    assert location.primary() == fallback
    monkeypatch.setattr(location, "_sandbox_mounts", lambda: (data, music))
    assert location.initial_root() == music
    assert location.primary() == fallback and location.load()["primary"] == str(fallback)


def test_publish_default_does_not_overwrite_a_concurrent_library_move(roots):
    _old, destination = roots
    published = location.request(destination)
    location.save(dict(version=1, primary="/stale-default", pending=[], aliases={}), only_if_missing=True)
    assert location.load() == published
