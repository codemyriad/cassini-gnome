# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the PR #130 reviews (codex and agy), one group per finding."""

import errno
import json
import os
import shutil
import struct
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")

from library_v2_support import (  # noqa: E402
    RATE,
    emulate_case_insensitive,
    meeting_json,
    new_session,
    root_files,
    rows,
    simple_session,
    track_audio,
)

from cassini_viewer import meeting_store, portable  # noqa: E402
from cassini_viewer.library_v2 import edit, fsutil, journal, mixdown, ogg, publish  # noqa: E402
from cassini_viewer.library_v2 import finalize as fin  # noqa: E402
from cassini_viewer.portable import ogg_crc  # noqa: E402

OLD = "2026-10-30--18.02-Weekly sync.opus"


@pytest.fixture(autouse=True)
def no_checkpoint(monkeypatch):
    monkeypatch.setattr(publish, "CHECKPOINT", None)


def finalize(root, sid, **kw):
    return fin.finalize(root, sid, zone="Europe/Rome", **kw)


def x_of(path):
    return portable.read_meeting(str(path)).manifest["x"]


def clean(root):
    layout = journal.Layout(Path(root))
    assert os.listdir(layout.journal) == [] and os.listdir(layout.staging) == []


def run_ahead(directory):
    """A transient write (§11 Q9): meeting.json moves to revision 2, the file stays at 1."""
    with meeting_store.LOCK:
        value = meeting_store.read_metadata(directory)
        value.update(revision=2, transcript=dict(status="queued", reference=None), etaMinutes=4)
        meeting_store.atomic_json(directory / "meeting.json", value)


# 1. the file's own revision is the expected base; files without session metadata --------------


def test_a_notes_edit_with_metadata_ahead_replaces_the_file(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    run_ahead(directory)
    assert edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=[dict(atMs=1, text="n")])) == OLD
    assert x_of(tmp_path / OLD)["codemyriad.io"]["revision"] == 3 and meeting_json(directory)["revision"] == 3
    assert x_of(tmp_path / OLD)["codemyriad.io"]["meeting"]["etaMinutes"] == 4  # the transient write lands too
    assert root_files(tmp_path) == [OLD]
    clean(tmp_path)


def test_a_title_edit_with_metadata_ahead_leaves_one_file(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    run_ahead(directory)
    name = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="Planning", titleSource="user"))
    assert root_files(tmp_path) == [name] == ["2026-10-30--18.02-Planning.opus"]
    assert meeting_json(directory)["revision"] == 3
    entry_revisions = x_of(tmp_path / name)["codemyriad.io"]["revision"]
    assert entry_revisions == 3
    clean(tmp_path)


def test_edits_of_a_file_without_its_session_folder_or_meeting_json(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    (directory / "meeting.json").unlink()
    name = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="Planning", titleSource="user"))
    rebuilt = meeting_json(directory)
    assert (rebuilt["revision"], rebuilt["title"], rebuilt["published"]) == (2, "Planning", True)
    assert rebuilt["namePrefix"] == "2026-10-30--18.02-"
    shutil.rmtree(directory)
    assert edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=[dict(atMs=2, text="m")])) == name
    assert x_of(tmp_path / name)["codemyriad.io"]["revision"] == 3 and not directory.exists()
    clean(tmp_path)


# 2. the own name stops being "free" once it was refused ------------------------------------


def test_a_refused_own_name_advances_the_suffix(tmp_path, monkeypatch):
    first = simple_session(tmp_path, 0.5, title="Alpha")[0]
    second, _ = simple_session(tmp_path, 0.5, title="Alpha")
    assert finalize(tmp_path, first).name.endswith("Alpha.opus")
    assert finalize(tmp_path, second).name.endswith("Alpha (2).opus")
    edit.edit_meeting(tmp_path, "Recording", first, dict(title="Beta", titleSource="user"))
    taken = tmp_path / "2026-10-30--18.02-Alpha.opus"

    def race(step):
        if step == "before-rename" and not taken.exists():
            taken.write_bytes(b"someone else")

    monkeypatch.setattr(publish, "CHECKPOINT", race)
    renamed = edit.edit_meeting(tmp_path, "Recording", second, dict(title="Alpha ", titleSource="user"))
    assert renamed == "2026-10-30--18.02-Alpha (3).opus"


def test_a_refused_case_variant_of_the_own_name_advances_the_suffix(tmp_path, monkeypatch):
    sid, _ = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    taken = tmp_path / "2026-10-30--18.02-WEEKLY SYNC.opus"

    def race(step):
        if step == "before-rename" and not taken.exists():
            taken.write_bytes(b"someone else")

    monkeypatch.setattr(publish, "CHECKPOINT", race)
    renamed = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="WEEKLY SYNC", titleSource="user"))
    assert renamed == "2026-10-30--18.02-WEEKLY SYNC (2).opus" and taken.read_bytes() == b"someone else"


# 3. a case-only rename on a case-insensitive filesystem never hides the meeting -------------


def case_failure(tmp_path, monkeypatch, fail):
    emulate_case_insensitive(monkeypatch.setattr)
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    fail(monkeypatch)
    with pytest.raises(publish.PendingCommit):
        edit.edit_meeting(tmp_path, "Recording", sid, dict(title="WEEKLY sync", titleSource="user"))
    assert len(os.listdir(tmp_path / ".cassini/journal")) == 1  # kept, with its stage
    monkeypatch.undo()
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    emulate_case_insensitive(monkeypatch.setattr)
    report = journal.recover(tmp_path)
    assert not report.kept and root_files(tmp_path) == ["2026-10-30--18.02-WEEKLY sync.opus"]
    assert meeting_json(directory)["revision"] == 2
    clean(tmp_path)


def test_a_failure_after_moving_the_original_aside_is_pending_not_discarded(tmp_path, monkeypatch):
    def fail(patch):
        rename = fsutil.rename_noreplace

        def failing(source, target):
            moved_aside = any(n.startswith(".cassini-case-") for n in os.listdir(Path(target).parent))
            if Path(source).name.endswith(".opus.stage") and moved_aside:
                raise OSError(errno.EIO, "Input/output error")
            return rename(source, target)

        patch.setattr(fsutil, "rename_noreplace", failing)

    case_failure(tmp_path, monkeypatch, fail)


def test_a_directory_fsync_failure_after_moving_the_original_aside_is_pending(tmp_path, monkeypatch):
    def fail(patch):
        real = fsutil.fsync_dir
        armed = []
        patch.setattr(publish, "CHECKPOINT", lambda step: armed.append(step) if step == "case-moved" else None)

        def fsync_dir(path):
            if armed:
                raise OSError(errno.EIO, "Input/output error")
            real(path)

        patch.setattr(fsutil, "fsync_dir", fsync_dir)

    case_failure(tmp_path, monkeypatch, fail)


def test_the_two_links_window_of_the_link_fallback_is_resolved(tmp_path, monkeypatch):
    """The link() fallback linked the original to its temporary name but died before unlinking it."""

    class Died(BaseException):
        pass

    def fail(patch):
        rename = fsutil.rename_noreplace

        def link_then_die(source, target):
            if Path(target).name.startswith(".cassini-case-"):
                os.link(source, target)
                raise Died()
            return rename(source, target)

        patch.setattr(fsutil, "rename_noreplace", link_then_die)

    case_failure(tmp_path, monkeypatch, fail)


# 5. builds: the marker goes last, and a lone starting marker is not an orphan ---------------


def test_build_close_unlinks_its_marker_last(tmp_path, monkeypatch):
    layout = journal.Layout(tmp_path)
    job = journal.Build(layout)
    marker = layout.staging / f"{job.op}.build.partial"
    for suffix in ("opus.stage", "mix.partial", "retag.partial", "x.partial", "y.partial"):
        (layout.staging / f"{job.op}.{suffix}").write_bytes(b"x")
    real = os.unlink
    seen = []

    def unlink(path, *a, **k):
        seen.append((Path(path).name, marker.exists()))
        real(path, *a, **k)

    listdir = os.listdir

    def marker_first(path):  # the worst order a directory listing can give
        return sorted(listdir(path), key=lambda name: name != marker.name)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "listdir", marker_first)
    job.close()
    monkeypatch.undo()
    assert seen[-1][0] == marker.name and all(present for name, present in seen if name != marker.name)
    assert os.listdir(layout.staging) == []


def test_a_lone_starting_marker_of_a_dead_build_is_cleaned_not_reported(tmp_path):
    layout = journal.Layout(tmp_path)
    layout.ensure()
    lone = layout.staging / "0f6d8e0a-6c1e-4d52-9a40-2c5f0f7f9a11.new.partial"
    lone.write_bytes(b"")
    report = journal.recover(tmp_path)
    assert report.orphans == [] and not lone.exists()


# 6. finished spans release their decoder and buffer ---------------------------------------


def proc_count(name):
    return len(os.listdir(f"/proc/self/{name}"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="needs /proc")
def test_forty_five_rotated_spans_keep_fds_threads_and_buffers_bounded(tmp_path):
    count = 45
    piece = track_audio(RATE, 0.5)
    spans = [
        dict(file=f"track-r-{i}.wav", sampleRate=RATE, channels=1, frames=len(piece), startOffsetMs=i * 500)
        for i in range(count)
    ]
    write = {f"track-r-{i}.wav": (piece, RATE, 1) for i in range(count)}
    _sid, directory = new_session(tmp_path, [dict(id="r", kind="remote", spans=spans, write=write)])
    timeline = mixdown.session_spans(directory, json.loads((directory / "session.json").read_text()))
    readers = [mixdown.Reader(span) for span in timeline.spans]
    before = proc_count("fd"), proc_count("task")
    peak_fd = peak_task = 0
    for _block in mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans), readers=readers):
        peak_fd, peak_task = max(peak_fd, proc_count("fd")), max(peak_task, proc_count("task"))
        assert all(len(r.buffer) <= 1 for r in readers)  # the consumed chunk is released
    assert peak_fd - before[0] <= 12 and peak_task - before[1] <= 16, (before, peak_fd, peak_task)


def test_anchored_reads_release_up_to_the_last_sample_used(tmp_path):
    audio = track_audio(RATE, 3.0)
    anchors = [[0, 0], [len(audio), int(3.0001 * 10**9)]]
    track = dict(id="a", kind="remote", file="track-a.wav", sampleRate=RATE, channels=1, frames=len(audio),
                 startOffsetMs=0, clockAnchors=anchors, write={"track-a.wav": (audio, RATE, 1)})  # fmt: skip
    _sid, directory = new_session(tmp_path, [track])
    timeline = mixdown.session_spans(directory, json.loads((directory / "session.json").read_text()))
    reader = mixdown.Reader(timeline.spans[0])
    for _block in mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans), readers=[reader]):
        assert len(reader.buffer) < 64  # was a whole second: the consumed chunk is released
    assert reader.done


# 7. malformed journal entries are kept and reported; recovery continues --------------------


def test_malformed_entries_are_kept_and_recovery_continues(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    layout = journal.Layout(tmp_path)
    bad = []
    for broken in (
        dict(kind="publish", phase="staged"),  # no stage
        dict(kind="rename", phase="staged", oldPath="../escape.opus"),
        dict(kind="delete", phase="intent", oldPath="a/b.opus"),
        dict(kind="replace", phase="staged", baseRevision="1"),
    ):
        entry = journal.new_entry(broken.pop("kind"), origin="Recording", session_id=sid,
                                  session=layout.relative(directory), **broken)  # fmt: skip
        (layout.journal / f"{entry['op']}.json").write_text(json.dumps(entry))
        bad.append(f"{entry['op']}.json")
    good = journal.new_entry("delete", origin="Recording", session_id=sid, phase="intent",
                             session=layout.relative(directory), oldPath=OLD, newPath=None)  # fmt: skip
    journal.write(layout, good)
    trashed = []
    publish.TRASH, real = trashed.append, publish.TRASH
    try:
        report = journal.recover(tmp_path)
    finally:
        publish.TRASH = real
    assert sorted(name for name, _reason in report.kept) == sorted(bad)
    assert report.completed == [good["op"]] and [p.name for p in trashed] == [OLD, directory.name]


# 8. delete never touches another meeting, and reconciles first --------------------------------


def test_a_delete_leaves_another_meetings_file_or_a_link_alone(tmp_path, monkeypatch):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)
    other_id, _ = simple_session(tmp_path, 0.5, title="Other")
    other = finalize(tmp_path, other_id).name
    layout = journal.Layout(tmp_path)
    (tmp_path / OLD).unlink()  # crashed after the file went to the Trash ...
    os.rename(tmp_path / other, tmp_path / OLD)  # ... and another meeting now has the name
    os.symlink(tmp_path / OLD, tmp_path / "2026-10-30--18.02-Link.opus")
    trashed, bin_ = [], tmp_path / "trash"
    bin_.mkdir()
    monkeypatch.setattr(publish, "TRASH", lambda path: (trashed.append(path), os.rename(path, bin_ / path.name)))
    for name in (OLD, "2026-10-30--18.02-Link.opus"):
        entry = journal.new_entry("delete", origin="Recording", session_id=sid, phase="intent",
                                  session=layout.relative(directory), oldPath=name, newPath=None)  # fmt: skip
        journal.write(layout, entry)
        report = journal.recover(tmp_path)
        assert report.completed == [entry["op"]] and not report.kept
    assert trashed == [directory] and publish.file_identity(tmp_path / OLD)[1] == other_id
    clean(tmp_path)


def test_delete_reconciles_a_pending_rename_first(tmp_path, monkeypatch):
    sid, directory = simple_session(tmp_path, 0.5)
    finalize(tmp_path, sid)

    class Died(BaseException):
        pass

    def die(step):
        if step == "renamed":
            raise Died()

    monkeypatch.setattr(publish, "CHECKPOINT", die)
    with pytest.raises(Died):
        edit.edit_meeting(tmp_path, "Recording", sid, dict(title="Renamed", titleSource="user"))
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(publish, "TRASH", lambda path: os.rename(path, trash / path.name))
    layout = journal.Layout(tmp_path)
    with journal.LibraryLock(layout):
        name = publish.find_file(layout, "Recording", sid)
        publish.delete(layout, origin="Recording", session_id=sid, file_name=name)
    assert journal.recover(tmp_path).kept == []
    assert rows(tmp_path) == {} and root_files(tmp_path) == []  # the old name did not come back
    clean(tmp_path)


# 9. only title edits recompute the displayed title -------------------------------------------


def test_a_notes_edit_keeps_the_untitled_string_of_first_publish(tmp_path):
    audio = track_audio(RATE, 0.5)
    track = dict(id="m", kind="microphone", file="track-m.wav", sampleRate=RATE, channels=1, frames=len(audio),
                 startOffsetMs=0, write={"track-m.wav": (audio, RATE, 1)})  # fmt: skip
    sid, _ = new_session(tmp_path, [track], title=None)
    name = finalize(tmp_path, sid, untitled="Riunione senza titolo").name
    assert name == "2026-10-30--18.02-Riunione senza titolo.opus"
    assert edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=[dict(atMs=1, text="n")])) == name
    assert portable.read_meeting(str(tmp_path / name)).tags["TITLE"] == "Riunione senza titolo"


def test_the_displayed_title_falls_back_to_the_received_name_stem():
    received = dict(originalName="Team call.opus", sha256="0" * 64, importedAtMs=1)
    assert edit.display_title(dict(title=None, received=received), None, "Untitled meeting") == "Team call"
    assert edit.display_title(dict(title=None, received=None), None, "Untitled meeting") == "Untitled meeting"


# 10. an EOS page must complete an audio packet ------------------------------------------------


def test_an_empty_trailing_eos_page_is_rejected(tmp_path):
    sid, _ = simple_session(tmp_path, 0.5)
    path = tmp_path / finalize(tmp_path, sid).name
    data = bytearray(path.read_bytes())
    last = data.rfind(b"OggS")
    header = bytearray(data[last : last + 27])
    lacing = data[last + 27 : last + 27 + header[26]]
    page = bytearray(header + lacing + data[last + 27 + header[26] :])
    page[5] &= ~4  # the old last page is no longer EOS
    struct.pack_into("<I", page, 22, 0)
    struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
    extra = bytearray(header[:26] + b"\x00")
    extra[5] = 4
    struct.pack_into("<I", extra, 18, struct.unpack_from("<I", header, 18)[0] + 1)
    struct.pack_into("<I", extra, 22, 0)
    struct.pack_into("<I", extra, 22, ogg_crc(bytes(extra)))
    path.write_bytes(bytes(data[:last]) + bytes(page) + bytes(extra))
    with pytest.raises(ogg.OggError, match="end-of-stream"):
        ogg.validate_path(path)
