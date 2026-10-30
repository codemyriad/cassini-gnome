# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §3/§4/§6/§7 and §9.2/§9.5: finalize, the published file, tag rewrites, failures, collisions."""

import contextlib
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

from library_v2_support import RATE, meeting_json, new_session, root_files, rows, simple_session, track_audio  # noqa: E402

from cassini_viewer import portable  # noqa: E402
from cassini_viewer.library_v2 import edit, fsutil, journal, mixdown, ogg, publish, xschema  # noqa: E402
from cassini_viewer.library_v2 import finalize as fin  # noqa: E402
from cassini_viewer.portable import ogg_crc  # noqa: E402

ZONE = "Europe/Rome"


@pytest.fixture(autouse=True)
def no_checkpoint(monkeypatch):
    monkeypatch.setattr(publish, "CHECKPOINT", None)


def finalize(root, sid, **kw):
    return fin.finalize(root, sid, zone=ZONE, **kw)


def manifest(path):
    return portable.read_meeting(str(path)).manifest


def digest(path):
    with open(path, "rb") as stream:
        return portable.compute_audio_digest(stream)


def clean_state(root):
    layout = journal.Layout(Path(root))
    assert os.listdir(layout.staging) == [] and os.listdir(layout.journal) == []
    assert not [n for n in os.listdir(root) if n.endswith((".stage", ".partial"))]


def test_finalize_publishes_a_valid_library_file_and_meeting_json_at_one_revision(tmp_path):
    notes = [dict(atMs=1000, text="Decide on Friday", extra={"kept": True})]
    sid, directory = simple_session(
        tmp_path,
        2.0,
        meeting=dict(version=1, revision=4, title=None, titleSource="meeting-window", notes=notes, speakers=[],
                     transcript=dict(status="queued", reference=None, model="whistle"), etaMinutes=3, error=None,
                     quoteOverride=None, received=None, macWindowFrame=[1, 2, 3, 4], big=2**60 + 1),
    )  # fmt: skip
    result = finalize(tmp_path, sid)
    assert result.name == "2026-10-30--18.02-Weekly sync.opus" and result.revision == 5
    assert root_files(tmp_path) == [result.name]
    clean_state(tmp_path)
    path = tmp_path / result.name
    ogg.validate_path(path)
    edit.verify(path)
    meeting = portable.read_meeting(str(path))
    value = meeting.manifest
    assert value["meeting"]["title"] == meeting.tags["TITLE"] == "Weekly sync"
    assert meeting.tags["DATE"] == "2026-10-30T18:02:41+01:00"
    assert value["meeting"]["recordedAtLocal"] == "2026-10-30T18:02:41"
    assert value["audio"]["channels"] == 1 and value["audio"]["sampleCount"] == 2 * RATE
    assert value["audioEncode"]["bitrateBps"] == 64000
    words = portable.load_words(meeting, "words")
    assert words == [] and meeting.tags["CASSINI_TX_WORDS_PAYLOAD_RAW_BYTES"] == "54"
    assert value["transcripts"][0]["wordCount"] == 0 and value["speakers"] == []
    assert value["x"]["gocassini.com"] == dict(notes=notes)
    ours = value["x"]["codemyriad.io"]
    assert xschema.valid(ours)
    assert ours["sessionId"] == sid and ours["origin"] == "Recording" and ours["revision"] == 5
    assert ours["namePrefix"] == "2026-10-30--18.02-" and ours["namePrefixSource"] == "recorded"
    assert ours["automaticTitle"] == "Weekly sync" and ours["meeting"]["title"] is None
    assert ours["passthrough"] == dict(macWindowFrame=[1, 2, 3, 4], big=2**60 + 1)
    stored = meeting_json(directory)
    assert stored["revision"] == 5 and stored["published"] is True
    assert stored["namePrefix"] == "2026-10-30--18.02-" and stored["namePrefixSource"] == "recorded"
    # MAPPING.md: the file rebuilds the same meeting.json.
    assert xschema.rebuild(ours, value["x"]["gocassini.com"]) == stored
    again = finalize(tmp_path, sid)
    assert again.already_published and again.name == result.name and root_files(tmp_path) == [result.name]


def test_x_schema_validator_agrees_with_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    meeting_schema = json.loads(Path(xschema.__file__).parent.parent.joinpath("meeting.schema.json").read_text())
    registry = referencing.Registry().with_resource(
        "https://x.invalid/meeting.schema.json", referencing.Resource.from_contents(meeting_schema)
    )
    schema = dict(xschema.SCHEMA, **{"$id": "https://x.invalid/library-v2/x-schema.json"})
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    example = schema["examples"][0]
    bad = [
        dict(example, schema=2),
        dict(example, passthrough=dict(namePrefixSource="recorded")),
        dict(example, origin="Received"),
        dict(example, tracks=[dict(id="m", spans=[dict(file="../x.wav", sampleRate=1, frames=0, startOffsetMs=0)])]),
        dict(example, tracks=[dict(id="m", spans=[dict(file="a.wav", sampleRate=1, frames=0, startOffsetMs=0, codec="opus")])]),
        dict(example, namePrefix="2026-10-30 18.02"),
    ]  # fmt: skip
    for value in [example, *bad]:
        assert xschema.valid(value) == validator.is_valid(value), value


def test_unknown_offsets_are_mixed_at_zero_and_marked(tmp_path):
    audio = track_audio(RATE, 1.0)
    track = dict(id="m", kind="microphone", file="track-m.wav", sampleRate=RATE, channels=1, frames=RATE,
                 startOffsetMs=None, write={"track-m.wav": (audio, RATE, 1)})  # fmt: skip
    sid, _ = new_session(tmp_path, [track], offset=None, created="2026-07-01T10:00:00Z")
    result = finalize(tmp_path, sid)
    value = manifest(tmp_path / result.name)
    assert result.offsets_assumed_zero
    assert value["x"]["gocassini.com"] == dict(recovery=dict(offsetsAssumedZero=True))
    # No recorded offset: the Library device's zone (CEST in July) and legacy-device-tz.
    assert result.name.startswith("2026-07-01--12.00-")
    assert value["x"]["codemyriad.io"]["namePrefixSource"] == "legacy-device-tz"
    assert portable.read_meeting(str(tmp_path / result.name)).tags["DATE"] == "2026-07-01T12:00:00+02:00"


# ---- §9.2 Ogg structure, two tag rewrites and a rename ----------------------------------


def pages(data):
    out, pos = [], 0
    while pos < len(data):
        nsegs = data[pos + 26]
        lacing = data[pos + 27 : pos + 27 + nsegs]
        end = pos + 27 + nsegs + sum(lacing)
        out.append((data[pos : pos + 27], lacing, data[pos + 27 + nsegs : end]))
        pos = end
    return out


def page(header, sequence, lacing, payload, flags=None):
    header = bytearray(header)
    if flags is not None:
        header[5] = flags
    struct.pack_into("<I", header, 18, sequence)
    header[26] = len(lacing)
    raw = bytearray(bytes(header[:22]) + bytes(4) + bytes([len(lacing)]) + lacing + payload)
    struct.pack_into("<I", raw, 22, ogg_crc(bytes(raw)))
    return bytes(raw)


def share_tags_page_with_audio(path):
    """Rewrite `path` so the page that ends OpusTags also carries the first audio page's packets."""
    found = pages(path.read_bytes())
    tags_end = next(i for i, (_h, lacing, _p) in enumerate(found) if i >= 1 and lacing[-1] != 255)
    head, lacing, payload = found[tags_end]
    audio_header, audio_lacing, audio_payload = found[tags_end + 1]
    assert len(lacing) + len(audio_lacing) <= 255
    merged = bytearray(audio_header)
    merged[5] = head[5]  # keep the continuation flag of the tags page
    out = [page(h, i, la, p) for i, (h, la, p) in enumerate(found[:tags_end])]
    out.append(page(bytes(merged), tags_end, lacing + audio_lacing, payload + audio_payload))
    out += [page(h, tags_end + 1 + i, la, p) for i, (h, la, p) in enumerate(found[tags_end + 2 :])]
    path.write_bytes(b"".join(out))


def audio_facts(path):
    facts = ogg.validate_path(path)
    return digest(path)["sha256"], facts.pre_skip, facts.final_granule, facts.audio_granules, facts.playable


def test_ogg_structure_survives_two_tag_rewrites_and_a_rename(tmp_path):
    sid, directory = simple_session(tmp_path, 3.0)
    first = finalize(tmp_path, sid)
    path = tmp_path / first.name
    reference = audio_facts(path)
    assert abs(reference[4] - 3 * RATE) <= mixdown.FRAME
    with open(path, "rb") as stream:
        original_vendor, original_fields = edit.comment_fields(stream)
    share_tags_page_with_audio(path)
    with pytest.raises(ogg.OggError, match="shares its last page"):
        ogg.validate_path(path)
    assert audio_facts_loose(path)[:3] == reference[:3]

    # Rewrite 1, on the input whose tags share a page with audio: a small notes edit.
    name = edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=[dict(atMs=5, text="hello")]))
    assert name == first.name and audio_facts(path) == reference
    assert meeting_json(directory)["revision"] == 2
    with open(path, "rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    assert vendor == original_vendor
    names = [f.split(b"=", 1)[0].upper() for f in fields]
    assert len(names) == len(set(names))  # replaced, never duplicated
    assert [f for f in fields if not f.upper().startswith(b"CASSINI_PAYLOAD_")] == [
        f for f in original_fields if not f.upper().startswith(b"CASSINI_PAYLOAD_")
    ]

    # Rewrite 2: the OpusTags packet grows past a 65,025-byte page.
    rng = np.random.default_rng(3)
    notes = [dict(atMs=i, text=rng.bytes(600).hex()) for i in range(200)]
    edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=notes))
    assert pages(path.read_bytes())[1][1] == bytes([255]) * 255  # tags fill page 1 and continue
    assert audio_facts(path) == reference
    assert manifest(path)["x"]["gocassini.com"]["notes"] == notes

    # A rename: the displayed title changes, the file keeps its identity and audio.
    renamed = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="Budget review", titleSource="user"))
    assert renamed == "2026-10-30--18.02-Budget review.opus" and root_files(tmp_path) == [renamed]
    after = tmp_path / renamed
    assert audio_facts(after) == reference
    meeting = portable.read_meeting(str(after))
    assert meeting.tags["TITLE"] == "Budget review" and meeting.manifest["meeting"]["title"] == "Budget review"
    assert meeting.manifest["meeting"]["id"] == "mtg_" + reference[0]
    ours = meeting.manifest["x"]["codemyriad.io"]
    assert (ours["sessionId"], ours["revision"], ours["meeting"]["title"]) == (sid, 4, "Budget review")
    assert meeting_json(directory)["revision"] == 4 and meeting_json(directory)["title"] == "Budget review"
    assert publish.find_file(journal.Layout(tmp_path), "Recording", sid) == renamed
    clean_state(tmp_path)


def audio_facts_loose(path):
    facts = ogg.validate_path(path, strict=False)
    return digest(path)["sha256"], facts.pre_skip, facts.final_granule


def test_rewrites_keep_unknown_tags_and_replace_case_insensitively():
    fields = [b"CASSINI_FORMAT=x", b"title=Old", b"X_EXAMPLE_COM_TICKET=OPS-14", b"cassini_payload_000=a",
              b"CASSINI_PAYLOAD_007=b", b"CASSINI_PAYLOAD_SCHEMA=s", b"CASSINI_PAYLOAD_SHA256=h", b"no-equals"]  # fmt: skip
    out = edit.edited_fields(fields, {"TITLE": "New", "CASSINI_PAYLOAD_000": "c", "CASSINI_PAYLOAD_SHA256": "i"})
    assert out == [b"CASSINI_FORMAT=x", b"X_EXAMPLE_COM_TICKET=OPS-14", b"CASSINI_PAYLOAD_SCHEMA=s", b"no-equals",
                   b"TITLE=New", b"CASSINI_PAYLOAD_000=c", b"CASSINI_PAYLOAD_SHA256=i"]  # fmt: skip


# ---- §9.5 failures and collisions ---------------------------------------------------------


class Stat:
    def __init__(self, free, total=500 << 30):
        self.f_frsize, self.f_blocks, self.f_bavail = 4096, total // 4096, free // 4096


def sources_intact(directory):
    return {p.name: p.read_bytes() for p in Path(directory).glob("track-*.wav")}


def test_low_disk_publishes_nothing_and_keeps_every_wav(tmp_path):
    sid, directory = simple_session(tmp_path, 1.0)
    before = sources_intact(directory)
    with pytest.raises(fin.LowDisk) as raised:
        finalize(tmp_path, sid, statvfs=lambda _p: Stat(free=10 << 30))  # 2 % of 500 GiB is the reserve
    assert raised.value.needed > 10 << 30
    assert root_files(tmp_path) == [] and sources_intact(directory) == before
    assert meeting_json(directory) is None
    clean_state(tmp_path)
    assert finalize(tmp_path, sid, statvfs=lambda _p: Stat(free=11 << 30)).name


@pytest.mark.parametrize("where", ["encode", "tags", "journal"])
def test_a_full_disk_before_the_rename_leaves_nothing_in_the_root(tmp_path, monkeypatch, where):
    sid, directory = simple_session(tmp_path, 1.0)
    before = sources_intact(directory)
    full = OSError(errno.ENOSPC, "No space left on device")
    if where == "encode":

        def encode(spans, output, choice, cancel=None):
            Path(output).write_bytes(b"OggS partial")
            raise full

        monkeypatch.setattr(mixdown, "encode", encode)
    elif where == "tags":

        def rewrite(*_a, **_k):
            raise full

        monkeypatch.setattr(fin, "rewrite_tags", rewrite)
    else:
        real = journal.write

        def write(layout, entry):
            if entry.get("phase") == "staged":
                raise full
            real(layout, entry)

        monkeypatch.setattr(journal, "write", write)
    with pytest.raises(OSError) as raised:
        finalize(tmp_path, sid)
    assert raised.value.errno == errno.ENOSPC
    assert root_files(tmp_path) == [] and sources_intact(directory) == before and meeting_json(directory) is None
    clean_state(tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    assert finalize(tmp_path, sid).name == "2026-10-30--18.02-Weekly sync.opus"


@pytest.mark.skipif(not os.environ.get("CASSINI_TINY_TMPFS"), reason="needs a tiny tmpfs (CASSINI_TINY_TMPFS)")
def test_a_really_full_tmpfs_leaves_nothing_in_the_root():
    root = Path(os.environ["CASSINI_TINY_TMPFS"]) / "library"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    sid, directory = simple_session(root, 1.0)
    before = sources_intact(directory)
    filler = root.parent / "filler"
    fd = os.open(filler, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        for size in (65536, 4096, 512):
            with contextlib.suppress(OSError):
                while os.write(fd, bytes(size)):
                    pass
    finally:
        os.close(fd)
    os.truncate(filler, max(0, os.path.getsize(filler) - 24 * 1024))  # leave ~24 KiB: journal fits, audio not
    try:
        with pytest.raises((OSError, ValueError)) as raised:
            finalize(root, sid, statvfs=lambda _p: Stat(free=100 << 30))
        # ENOSPC from a write, or the encoder's filesink error that reports it.
        assert getattr(raised.value, "errno", None) == errno.ENOSPC or "space" in str(raised.value).lower()
        assert root_files(root) == [] and sources_intact(directory) == before
        clean_state(root)
    finally:
        filler.unlink()
    assert finalize(root, sid, statvfs=lambda _p: Stat(free=100 << 30)).name


def test_a_directory_fsync_failure_after_the_rename_is_a_pending_commit(tmp_path, monkeypatch):
    sid, directory = simple_session(tmp_path, 1.0)
    real = fsutil.fsync_dir
    armed = []
    monkeypatch.setattr(publish, "CHECKPOINT", lambda step: armed.append(step) if step == "renamed" else None)

    def fsync_dir(path):
        if armed:
            raise OSError(errno.EIO, "Input/output error")
        real(path)

    monkeypatch.setattr(fsutil, "fsync_dir", fsync_dir)
    with pytest.raises(publish.PendingCommit):
        finalize(tmp_path, sid)
    # Published but not committed: the file, the journal entry and every source are kept.
    assert root_files(tmp_path) == ["2026-10-30--18.02-Weekly sync.opus"]
    assert len(os.listdir(tmp_path / ".cassini/journal")) == 1 and meeting_json(directory) is None
    assert sources_intact(directory)
    monkeypatch.setattr(fsutil, "fsync_dir", real)
    report = journal.recover(tmp_path)
    assert len(report.completed) == 1 and not report.kept
    assert meeting_json(directory)["published"] is True and meeting_json(directory)["revision"] == 1
    clean_state(tmp_path)
    assert rows(tmp_path) == {("Recording", sid): [("file", "2026-10-30--18.02-Weekly sync.opus", 1)]}


def test_zero_sample_sources_are_reported_and_an_all_silent_session_is_not_published(tmp_path):
    audio = track_audio(RATE, 1.0)
    tracks = [
        dict(id="mic", kind="microphone", file="track-mic.wav", sampleRate=RATE, channels=1, frames=RATE,
             startOffsetMs=0, write={"track-mic.wav": (audio, RATE, 1)}),
        dict(id="sys", kind="system", file="track-sys.wav", sampleRate=RATE, channels=1, frames=0,
             startOffsetMs=None, write={"track-sys.wav": (np.zeros(0), RATE, 1)}),
    ]  # fmt: skip
    sid, _ = new_session(tmp_path, tracks)
    result = finalize(tmp_path, sid)
    assert result.missing_sources == ["sys"] and not result.offsets_assumed_zero
    silent = [dict(tracks[1], id="x", file="track-x.wav", write={"track-x.wav": (np.zeros(0), RATE, 1)})]
    sid2, directory2 = new_session(tmp_path, silent)
    with pytest.raises(mixdown.EmptyRecording):
        finalize(tmp_path, sid2)
    assert meeting_json(directory2) is None and root_files(tmp_path) == [result.name]
    clean_state(tmp_path)


def test_a_missing_or_damaged_span_publishes_nothing(tmp_path):
    sid, directory = simple_session(tmp_path, 1.0)
    (directory / "track-microphone.wav").write_bytes(b"RIFF")
    with pytest.raises(ValueError):
        finalize(tmp_path, sid)
    assert root_files(tmp_path) == [] and meeting_json(directory) is None
    clean_state(tmp_path)


def test_same_minute_same_title_gets_the_next_suffix(tmp_path):
    names = [finalize(tmp_path, simple_session(tmp_path, 0.5)[0]).name for _ in range(3)]
    assert names == [
        "2026-10-30--18.02-Weekly sync.opus",
        "2026-10-30--18.02-Weekly sync (2).opus",
        "2026-10-30--18.02-Weekly sync (3).opus",
    ]
    # Case and normalization variants count as taken (§11 Q5).
    (tmp_path / "2026-10-30--18.02-weekly sync (4).opus").write_bytes(b"")
    assert finalize(tmp_path, simple_session(tmp_path, 0.5)[0]).name.endswith("Weekly sync (5).opus")


def test_eexist_at_the_rename_takes_the_next_suffix_and_gives_up_after_99_retries(tmp_path, monkeypatch):
    sid, directory = simple_session(tmp_path, 0.5)

    def race(step):
        if step == "before-rename" and not (tmp_path / "2026-10-30--18.02-Weekly sync.opus").exists():
            (tmp_path / "2026-10-30--18.02-Weekly sync.opus").write_bytes(b"someone else")

    monkeypatch.setattr(publish, "CHECKPOINT", race)
    assert finalize(tmp_path, sid).name == "2026-10-30--18.02-Weekly sync (2).opus"
    assert (tmp_path / "2026-10-30--18.02-Weekly sync.opus").read_bytes() == b"someone else"

    sid2, directory2 = simple_session(tmp_path, 0.5)
    attempts = []

    def always_exists(old, new):
        attempts.append(new)
        return -1, errno.EEXIST

    monkeypatch.setattr(publish, "CHECKPOINT", None)
    monkeypatch.setattr(fsutil, "_native", always_exists)
    with pytest.raises(publish.NamesExhausted):
        finalize(tmp_path, sid2)
    assert len(attempts) == 100 and len(set(attempts)) == 100
    assert len(root_files(tmp_path)) == 2 and meeting_json(directory2) is None
    clean_state(tmp_path)


def test_link_fallback_without_renameat2_never_replaces(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "_native", lambda _old, _new: (-1, errno.ENOSYS))
    first = finalize(tmp_path, simple_session(tmp_path, 0.5)[0]).name
    second = finalize(tmp_path, simple_session(tmp_path, 0.5)[0]).name
    assert (first, second) == ("2026-10-30--18.02-Weekly sync.opus", "2026-10-30--18.02-Weekly sync (2).opus")
    assert os.stat(tmp_path / first).st_nlink == 1
    clean_state(tmp_path)
    stage = tmp_path / "x.stage"
    stage.write_bytes(b"a")
    with pytest.raises(FileExistsError):
        fsutil.rename_noreplace(stage, tmp_path / first)
    assert stage.exists() and edit.verify(tmp_path / first)


def test_renames_onto_an_existing_name_and_onto_its_own_name(tmp_path):
    one = simple_session(tmp_path, 0.5, title="Alpha")[0]
    two, two_dir = simple_session(tmp_path, 0.5, title="Beta")
    a, b = finalize(tmp_path, one).name, finalize(tmp_path, two).name
    alpha = (tmp_path / a).read_bytes()
    # Onto an existing name: the next suffix, the other meeting untouched.
    renamed = edit.edit_meeting(tmp_path, "Recording", two, dict(title="Alpha", titleSource="user"))
    assert renamed == "2026-10-30--18.02-Alpha (2).opus" and (tmp_path / a).read_bytes() == alpha
    assert not (tmp_path / b).exists()
    # Onto its own name: same file name after sanitizing, so a replace in place (§11 Q10).
    same = edit.edit_meeting(tmp_path, "Recording", two, dict(title="Alpha ", titleSource="user"))
    assert same == renamed and manifest(tmp_path / same)["meeting"]["title"] == "Alpha "
    # Back to its own base name, now free of the suffix only if the base is free: it is not.
    again = edit.edit_meeting(tmp_path, "Recording", two, dict(title="Alpha", titleSource="user"))
    assert again == renamed
    assert sorted(root_files(tmp_path)) == sorted([a, renamed]) and meeting_json(two_dir)["revision"] == 4
    clean_state(tmp_path)


def test_a_200_byte_name_plus_suffix_stays_within_200_bytes(tmp_path):
    title = "z" * 300
    first = finalize(tmp_path, simple_session(tmp_path, 0.5, title=title)[0]).name
    second = finalize(tmp_path, simple_session(tmp_path, 0.5, title=title)[0]).name
    assert len(first.encode()) == 200 and len(second.encode()) == 200 and second.endswith("z (2).opus")
    full = manifest(tmp_path / second)
    assert full["meeting"]["title"] == title  # metadata keeps the full title; only the name is cut


def test_lock_excludes_a_second_holder_and_journal_partials_are_dropped(tmp_path):
    layout = journal.Layout(tmp_path)
    with journal.LibraryLock(layout), pytest.raises(journal.LockBusy), journal.LibraryLock(layout, blocking=False):
        pass
    (layout.journal / "abc.json.partial").write_text("{")
    (layout.journal / "junk.json").write_text("{")
    report = journal.recover(tmp_path)
    assert not (layout.journal / "abc.json.partial").exists()
    assert report.kept == [("junk.json", "damaged journal entry")] and (layout.journal / "junk.json").exists()


def test_a_conflicting_staged_replace_is_kept_not_applied(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    name = finalize(tmp_path, sid).name
    layout = journal.Layout(tmp_path)
    original = (tmp_path / name).read_bytes()
    entry = journal.new_entry("replace", origin="Recording", session_id=sid, session=layout.relative(directory),
                              oldPath=name, newPath=name, baseRevision=7, newRevision=8, name=None,
                              meetingUpdate=dict(title="x"), phase="staged")  # fmt: skip
    entry["stage"] = f".cassini/staging/{entry['op']}.opus.stage"
    (tmp_path / entry["stage"]).write_bytes(b"newer edit")
    journal.write(layout, entry)
    report = journal.recover(tmp_path)
    assert len(report.kept) == 1 and (tmp_path / entry["stage"]).exists()
    assert (tmp_path / name).read_bytes() == original and meeting_json(directory)["revision"] == 1
    assert journal.recover(tmp_path).kept  # kept on every start until resolved, never deleted


def test_mapping_round_trips_presence_of_name_prefix_source_and_unknown_x_members():
    meeting = dict(version=1, id="8b7d3c2e-5f1a-4c9e-9a6b-1d2e3f4a5b6c", revision=3, namePrefix="2026-10-30--18.02-",
                   title="T", titleSource="user", notes=[], speakers=[], transcript=dict(status="none", reference=None),
                   etaMinutes=None, error=None, quoteOverride=None, received=None, published=True, other={"a": 1})  # fmt: skip
    x = xschema.private_data(meeting, origin="Recording", automatic_title=None, base=dict(futureMember=[1]))
    assert "namePrefixSource" not in x and x["passthrough"] == dict(other={"a": 1}) and x["futureMember"] == [1]
    assert xschema.valid(x) and xschema.rebuild(x) == meeting
    x2 = xschema.private_data(
        dict(meeting, namePrefixSource="legacy-device-tz"), origin="Recording", automatic_title=None
    )
    assert x2["namePrefixSource"] == "legacy-device-tz" and "namePrefixSource" not in x2["passthrough"]


# ---- §11 step-3a answers: case-only renames (Q7), no lock through the mixdown (Q10), Q6 ----


def test_a_case_only_rename_keeps_the_base_name(tmp_path):
    sid, directory = simple_session(tmp_path, 0.5)
    first = finalize(tmp_path, sid).name
    second = finalize(tmp_path, simple_session(tmp_path, 0.5)[0]).name
    assert (first, second) == ("2026-10-30--18.02-Weekly sync.opus", "2026-10-30--18.02-Weekly sync (2).opus")
    renamed = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="weekly SYNC", titleSource="user"))
    assert renamed == "2026-10-30--18.02-weekly SYNC.opus"  # its own name is free under the casefold key
    assert sorted(root_files(tmp_path)) == sorted([renamed, second])
    clean_state(tmp_path)


@pytest.fixture
def case_insensitive(monkeypatch):
    """Make the Library behave like a case-insensitive filesystem (names resolve by casefold key)."""
    from cassini_viewer.library_v2 import names

    real_rename, real_same = fsutil.rename_noreplace, fsutil.same_file

    def resolve(path):
        path = Path(path)
        match = [n for n in os.listdir(path.parent) if names.fold(n) == names.fold(path.name)]
        return path.parent / match[0] if match else path

    def rename(source, target):
        if resolve(target).exists():
            raise FileExistsError(errno.EEXIST, "File exists", str(target))
        return real_rename(source, target)

    monkeypatch.setattr(fsutil, "rename_noreplace", rename)
    monkeypatch.setattr(fsutil, "same_file", lambda a, b: real_same(resolve(a), resolve(b)))


def test_a_case_only_rename_on_a_case_insensitive_filesystem_goes_through_a_temporary_name(
    tmp_path, monkeypatch, case_insensitive
):
    sid, directory = simple_session(tmp_path, 0.5)
    old = finalize(tmp_path, sid).name
    steps = []
    monkeypatch.setattr(publish, "CHECKPOINT", steps.append)
    renamed = edit.edit_meeting(tmp_path, "Recording", sid, dict(title="WEEKLY sync", titleSource="user"))
    assert renamed == "2026-10-30--18.02-WEEKLY sync.opus" and old == "2026-10-30--18.02-Weekly sync.opus"
    assert root_files(tmp_path) == [renamed] and not [n for n in os.listdir(tmp_path) if n.startswith(".cassini-")]
    assert steps.index("case-temp") < steps.index("renamed") < steps.index("after-remove-old")
    assert manifest(tmp_path / renamed)["x"]["codemyriad.io"]["revision"] == 2
    assert meeting_json(directory)["title"] == "WEEKLY sync"
    clean_state(tmp_path)


def paused_at(step_name, worker_name):
    """A CHECKPOINT that holds the named worker thread at `step_name` until released."""
    import threading

    reached, release = threading.Event(), threading.Event()

    def hook(step):
        if step == step_name and threading.current_thread().name == worker_name:
            reached.set()
            assert release.wait(30)

    return hook, reached, release


def run_thread(name, function):
    import threading

    box = {}

    def target():
        try:
            box["value"] = function()
        except BaseException as error:  # reported by the test
            box["error"] = error

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread, box


def test_another_meeting_can_be_renamed_while_a_mixdown_runs(tmp_path, monkeypatch):
    other = simple_session(tmp_path, 0.5, title="Other")[0]
    finalize(tmp_path, other)
    sid, _ = simple_session(tmp_path, 1.0)
    hook, reached, release = paused_at("mixed", "mixdown")
    monkeypatch.setattr(publish, "CHECKPOINT", hook)
    mixing, result = run_thread("mixdown", lambda: finalize(tmp_path, sid))
    assert reached.wait(30)
    renaming, renamed = run_thread("rename", lambda: edit.edit_meeting(tmp_path, "Recording", other, dict(title="Renamed", titleSource="user")))  # fmt: skip
    renaming.join(15)
    finished = not renaming.is_alive()
    release.set()
    mixing.join(30)
    renaming.join(30)
    assert finished, "the rename waited for the mixdown: the Library lock was held"
    assert renamed["value"] == "2026-10-30--18.02-Renamed.opus" and "error" not in result
    assert sorted(root_files(tmp_path)) == ["2026-10-30--18.02-Renamed.opus", "2026-10-30--18.02-Weekly sync.opus"]
    clean_state(tmp_path)


def test_commit_retags_when_meeting_json_changed_during_the_mixdown(tmp_path, monkeypatch):
    from cassini_viewer import meeting_store

    sid, directory = simple_session(tmp_path, 1.0)
    hook, reached, release = paused_at("mixed", "mixdown")
    monkeypatch.setattr(publish, "CHECKPOINT", hook)
    mixing, result = run_thread("mixdown", lambda: finalize(tmp_path, sid))
    assert reached.wait(30)
    with meeting_store.LOCK:
        value = meeting_store.read_metadata(directory)
        meeting_store.atomic_json(
            directory / "meeting.json", dict(value, title="Changed", titleSource="user", revision=1)
        )
    release.set()
    mixing.join(30)
    assert result["value"].name == "2026-10-30--18.02-Changed.opus" and result["value"].revision == 2
    ours = manifest(tmp_path / result["value"].name)["x"]["codemyriad.io"]
    assert (ours["revision"], ours["meeting"]["title"]) == (2, "Changed")
    assert meeting_json(directory)["revision"] == 2 and meeting_json(directory)["published"] is True
    clean_state(tmp_path)


def test_a_second_finalize_of_a_published_meeting_is_idempotent(tmp_path, monkeypatch):
    """Q6: startup may finalize again; a racing build sees the commit and publishes nothing new."""
    sid, directory = simple_session(tmp_path, 1.0)
    hook, reached, release = paused_at("stage-written", "late")
    monkeypatch.setattr(publish, "CHECKPOINT", hook)
    late, result = run_thread("late", lambda: finalize(tmp_path, sid))
    assert reached.wait(30)
    first = finalize(tmp_path, sid)
    release.set()
    late.join(30)
    assert result["value"].already_published and result["value"].name == first.name
    assert root_files(tmp_path) == [first.name] and meeting_json(directory)["revision"] == 1
    clean_state(tmp_path)


def test_recovery_removes_only_builds_whose_process_is_gone(tmp_path):
    layout = journal.Layout(tmp_path)
    with journal.Build(layout) as running:
        running.stage.write_bytes(b"half")
        report = journal.recover(tmp_path)
        assert running.stage.exists() and not report.orphans  # a live build is left alone
    assert os.listdir(layout.staging) == []
    (layout.staging / "5b0c7ad3-0d1f-4a7e-9d43-3a0d3b7e1a11.opus.stage").write_bytes(b"x")
    assert journal.recover(tmp_path).orphans == ["5b0c7ad3-0d1f-4a7e-9d43-3a0d3b7e1a11.opus.stage"]
