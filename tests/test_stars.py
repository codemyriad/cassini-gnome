# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-device Library stars (L-34, D99, D53.1): durable, merged, never in a Cassini file."""

import json
import threading

import pytest

from cassini_talk import stars
from cassini_talk.stars import StarStore


def test_update_is_durable_sorted_and_merges_with_what_is_on_disk(tmp_path):
    assert stars.read(tmp_path) == frozenset()
    stars.update(tmp_path, "Recording-b", True)
    stars.update(tmp_path, "Recording-a", True)
    path = tmp_path / ".cassini" / "stars.json"
    assert json.loads(path.read_text()) == {"version": 1, "starred": ["Recording-a", "Recording-b"]}
    assert stars.update(tmp_path, "Recording-b", False) == frozenset({"Recording-a"})
    assert stars.read(tmp_path) == frozenset({"Recording-a"})
    # Atomic replace: no temporary sibling survives a write.
    assert sorted(p.name for p in path.parent.iterdir()) == ["stars.json", "stars.lock"]


@pytest.mark.parametrize(
    "data",
    [b"", b"{", b"\xff\xfe", b"[]", b'{"version": 2, "starred": ["x"]}', b'{"version": 1, "starred": "x"}'],
)
def test_unreadable_file_reads_as_no_stars_and_the_next_star_replaces_it(tmp_path, data):
    (tmp_path / ".cassini").mkdir()
    (tmp_path / ".cassini" / "stars.json").write_bytes(data)
    assert stars.read(tmp_path) == frozenset()
    assert stars.update(tmp_path, "file/abc", True) == frozenset({"file/abc"})


def test_parse_drops_non_string_and_oversized_entries():
    data = json.dumps({"version": 1, "starred": ["ok", 3, None, "", "x" * 513]}).encode()
    assert stars.parse(data) == frozenset({"ok"})
    assert stars.parse(b" " * (stars.MAX_BYTES + 1)) == frozenset()


def test_concurrent_writers_never_lose_a_star(tmp_path):
    keys = [f"Recording-{i:04d}" for i in range(40)]
    gate = threading.Barrier(len(keys))

    def star(key):
        gate.wait()
        stars.update(tmp_path, key, True)

    threads = [threading.Thread(target=star, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert stars.read(tmp_path) == frozenset(keys)


def test_store_updates_at_once_and_adopts_disk_state_only_when_nothing_newer_is_queued(tmp_path):
    stars.update(tmp_path, "Recording-old", True)
    changes = []
    store = StarStore(tmp_path, on_changed=lambda s: changes.append(s.starred))
    # A star set before the initial read finishes must survive that read.
    store.set("Recording-new", True)
    assert store.is_starred("Recording-new")
    store.flush()
    assert store.loaded
    assert store.starred == frozenset({"Recording-old", "Recording-new"})
    assert store.toggle("Recording-old") is False
    store.flush()
    assert store.starred == frozenset({"Recording-new"}) == stars.read(tmp_path)
    assert changes[-1] == frozenset({"Recording-new"})
    store.close()
    store.set("Recording-late", True)
    assert not store.is_starred("Recording-late"), "a closed store ignores changes"


def test_a_moved_library_carries_its_stars_to_the_new_location(tmp_path):
    old, new = tmp_path / "Old", tmp_path / "New"
    stars.update(new, "Recording-there", True)
    previous = StarStore(old)
    previous.set("Recording-moved", True)
    previous.flush()
    moved = StarStore(new, inherit=previous.starred)
    previous.close()
    assert moved.is_starred("Recording-moved"), "carried stars show before the new file is read"
    moved.flush()
    assert moved.starred == frozenset({"Recording-moved", "Recording-there"}) == stars.read(new)
    moved.close()
    before = (new / ".cassini" / "stars.json").stat().st_mtime_ns
    assert stars.merge(new, {"Recording-moved"}) == stars.read(new)
    assert (new / ".cassini" / "stars.json").stat().st_mtime_ns == before, "nothing new, nothing written"


def test_store_shows_the_stored_stars_when_the_write_fails(tmp_path, monkeypatch):
    store = StarStore(tmp_path)
    store.flush()

    def refuse(*_args):
        raise PermissionError("read-only Library")

    monkeypatch.setattr(stars.fsutil, "write_durable", refuse)
    store.set("Recording-x", True)
    store.flush()
    assert not store.is_starred("Recording-x")
    store.close()


# --- Library integration: the key follows the meeting, never the file bytes ---------------


def test_star_survives_publication_and_rename_and_never_enters_files_or_exports(tmp_path, monkeypatch):
    import zipfile

    gi = pytest.importorskip("gi")
    pytest.importorskip("numpy")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication tests require GStreamer")

    from library_v2_support import simple_session

    from cassini_talk.exporting import export_meeting
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_v2 import finalize
    from cassini_talk.meeting_rename import rename_meeting
    from cassini_talk.zip_export import export_meetings_zip

    root = tmp_path / "Library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sid, directory = simple_session(root, 0.1)
    scanner = LibraryScanner(root)
    (recording,) = scanner.scan(capture_activity={sid: "recording"}).meetings
    key = stars.star_key(recording)
    assert key == f"Recording-{sid}"
    stars.update(root, key, True)

    # Session -> published .opus: same Library key, no migration needed.
    result = finalize.finalize(root, sid, zone="Europe/Rome")
    (published,) = scanner.scan().meetings
    assert published.published_file == root / result.name
    assert stars.star_key(published) == key and key in stars.read(root)

    # Rename rewrites the published file name and title; the star stays.
    rename_meeting(directory, "Renamed budget review")
    (renamed,) = LibraryScanner(root).scan().meetings
    assert renamed.title == "Renamed budget review"
    assert stars.star_key(renamed) == key and key in stars.read(root)

    # D53.1: the star is Library state only; no meeting byte or export carries it.
    marker = key.encode()
    stars_bytes = (root / ".cassini" / "stars.json").read_bytes()
    assert marker in stars_bytes
    for path in root.glob("*.opus"):
        data = path.read_bytes()
        assert b"stars.json" not in data and b'"starred"' not in data and b"starred" not in data.lower()
    (tmp_path / "Exports").mkdir()
    (tmp_path / "Zips").mkdir()
    exported = export_meeting(renamed, tmp_path / "Exports")
    assert b"starred" not in exported.read_bytes().lower()
    archive = export_meetings_zip([renamed], tmp_path / "Zips")
    with zipfile.ZipFile(archive) as bundle:
        assert all("star" not in name.lower() for name in bundle.namelist())
        assert all(b"starred" not in bundle.read(name).lower() for name in bundle.namelist())
    # Stars never make a scanner see an extra meeting.
    assert [m.key for m in LibraryScanner(root).scan().meetings] == [key]
