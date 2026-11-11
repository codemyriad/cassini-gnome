# SPDX-License-Identifier: GPL-3.0-or-later
"""D95: file truth, seal time, timezone-free labels and round-trip provenance."""

import json
import shutil
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cassini_talk import meeting_time, portable, ux_formats
from cassini_talk.export_model import markdown
from cassini_talk.export_model import model_projection as projection
from cassini_talk.library import LibraryScanner, LibrarySnapshot
from cassini_talk.open_meeting import load_open_meeting
from cassini_talk.portable_writer import MANIFEST_MIME, chunk_set, descriptors, rewrite_tags
from cassini_talk.search_index import SearchIndex

VECTORS = Path(__file__).parent / "conformance"
START = datetime(2026, 11, 9, 9, 31, 48, 123000, UTC)


def changed_file(tmp_path, *, recorded=None, date=None, local=None, created="2026-11-09T09:40:48Z"):
    source = VECTORS / "opus/001-minimal-v1.opus"
    document = portable.read_meeting(str(source))
    manifest = deepcopy(document.manifest)
    manifest["meeting"]["createdAtUtc"] = created
    for key, value in (("recordedAtUtc", recorded), ("recordedAtLocal", local)):
        manifest["meeting"].pop(key, None)
        if value is not None:
            manifest["meeting"][key] = value
    tags = dict(document.tags)
    tags["DATE"] = date or created
    payload, _ = chunk_set("CASSINI_PAYLOAD_", manifest, MANIFEST_MIME)
    tags.update(payload)
    target = tmp_path / "Synthetic English meeting.opus"
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        rewrite_tags(incoming, outgoing, tags)
    return target


@pytest.mark.parametrize("name", ["027-recorded-at-utc", "028-date-is-created-at", "029-date-with-offset"])
def test_new_vectors_rebuild_catalogue_and_open_model_without_seal_time(tmp_path, name):
    fixture = next(v for v in json.loads((VECTORS / "index.json").read_text())["vectors"] if v["id"] == name)
    root = tmp_path / "Library"
    root.mkdir()
    target = root / (name + ".opus")
    shutil.copyfile(VECTORS / fixture["file"], target)
    embedded = portable.read_meeting(str(target))
    expected = fixture["expect"]["meetingStart"]
    assert embedded.meeting_start == expected
    opened = load_open_meeting(target, tmp_path / "Elsewhere")
    (rebuilt,) = LibraryScanner(root).scan().meetings
    for meeting in (opened, rebuilt):
        if expected["source"] == "recordedAtLocal":
            assert meeting.started_at_ms is None
            assert meeting.display_start == expected["value"]
            assert ux_formats.row_time(meeting.display_start, tz="Pacific/Honolulu") == "09:31"
            assert ux_formats.row_time(meeting.display_start, tz="Asia/Tokyo") == "09:31"
        else:
            assert meeting.started_at_ms == meeting_time.milliseconds(expected["value"])
            assert ux_formats.row_time(meeting.display_start, tz="UTC") == "09:31"


def test_seal_only_is_never_a_display_or_search_date(tmp_path):
    target = changed_file(tmp_path)
    meeting = load_open_meeting(target)
    assert meeting.display_start is None
    assert meeting.sort_time == meeting_time.milliseconds("2026-11-09T09:40:48Z")
    text = markdown(projection(meeting))
    assert "2026" not in text and "09:40" not in text
    with closing(SearchIndex(tmp_path / "Library", tmp_path / "cache")) as index:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        assert index.query("One", now_ms=START.timestamp() * 1000, tz="UTC")
        assert not index.query("One", date="today", now_ms=START.timestamp() * 1000, tz="UTC")


def test_local_label_stays_in_its_calendar_day_for_search_and_markdown(tmp_path):
    target = changed_file(tmp_path, local="2026-11-09T00:01:00")
    meeting = load_open_meeting(target)
    with closing(SearchIndex(tmp_path / "Library", tmp_path / "cache")) as index:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        for zone in ["Pacific/Honolulu", "Asia/Tokyo"]:
            # Noon in either viewer zone is 9 November; label must not move days.
            from zoneinfo import ZoneInfo

            now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo(zone)).timestamp() * 1000
            assert index.query("One", date="today", now_ms=now, tz=zone)
    output = markdown(projection(meeting, tz="Asia/Tokyo"))
    assert "00:01" in output and "09:40" not in output


def test_recording_sealed_nine_minutes_later_round_trips_at_start(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from library_v2_support import simple_session

    from cassini_talk.exporting import export_meeting
    from cassini_talk.library_v2.finalize import finalize
    from cassini_talk.received import import_received

    root = tmp_path / "Original"
    root.mkdir()
    sid, _ = simple_session(root, 0.1, created=START.isoformat().replace("+00:00", "Z"))
    result = finalize(root, sid, zone="Europe/Rome", now=START + timedelta(minutes=9))
    path = root / result.name
    document = portable.read_meeting(str(path))
    value = document.manifest["meeting"]
    assert meeting_time.milliseconds(value["recordedAtUtc"]) == int(START.timestamp() * 1000)
    assert value["createdAtUtc"] == "2026-11-09T09:40:48.123Z"
    assert document.tags["CASSINI_RECORDED_AT_UTC"] == document.tags["DATE"] == value["recordedAtUtc"]
    imported = import_received(path, tmp_path / "Received")
    # import_received returns the folder; derive the same model the Library uses.
    (meeting,) = LibraryScanner(tmp_path / "Received").scan().meetings
    assert meeting.started_at_ms == int(START.timestamp() * 1000)
    exported = export_meeting(meeting, tmp_path / "Exports")
    again = portable.read_meeting(str(exported))
    assert again.recorded_at_ms == int(START.timestamp() * 1000)
    assert again.tags["DATE"] == again.tags["CASSINI_RECORDED_AT_UTC"] == again.manifest["meeting"]["recordedAtUtc"]
    assert imported


@pytest.mark.parametrize("offset", [None, -240])
def test_raw_capture_utc_is_known_even_when_site_zone_is_unknown(tmp_path, offset):
    source = load_open_meeting(VECTORS / "opus/001-minimal-v1.opus")
    meeting = replace(
        source, origin="own", started_at_ms=int(START.timestamp() * 1000), recording_offset_minutes=offset
    )
    with meeting.audio_file.open("rb") as audio:
        digest = portable.compute_audio_digest(audio)
    tags, manifest = descriptors(meeting, digest, tz="Asia/Tokyo")
    assert meeting_time.milliseconds(manifest["meeting"]["recordedAtUtc"]) == int(START.timestamp() * 1000)
    assert tags["DATE"] == tags["CASSINI_RECORDED_AT_UTC"] == manifest["meeting"]["recordedAtUtc"]
    assert ("recordedAtLocal" in manifest["meeting"]) == (offset is not None)


@pytest.mark.parametrize(
    "date",
    ["2026-11-09T09:40:48Z", "2026-11-09T10:40:48+01:00", "2026-11-09T09:40:48.000Z"],
)
def test_same_seal_instant_in_different_offset_or_precision_is_not_meeting_time(date):
    manifest = {"meeting": {"createdAtUtc": "2026-11-09T09:40:48Z", "recordedAtLocal": "2026-11-08T20:01:00"}}
    assert meeting_time.selected(manifest, {"DATE": date}) == {
        "source": "recordedAtLocal",
        "value": "2026-11-08T20:01:00",
    }


@pytest.mark.parametrize(
    "date", ["2026", "2026-11-09T09:31:48", "2026-11-09T09:31:48-00:00", "2026-11-09T09:31:48+01:60"]
)
def test_non_instant_date_does_not_replace_local_label(date):
    assert meeting_time.selected({"meeting": {"recordedAtLocal": "2026-11-08T20:01:00"}}, {"DATE": date}) == {
        "source": "recordedAtLocal",
        "value": "2026-11-08T20:01:00",
    }


def test_local_label_change_invalidates_only_metadata_in_search_result(tmp_path):
    meeting = load_open_meeting(changed_file(tmp_path, local="2026-11-08T20:01:00"))
    today = START.timestamp() * 1000
    with closing(SearchIndex(tmp_path / "Library", tmp_path / "cache")) as index:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        assert not index.query("One", date="today", now_ms=today, tz="UTC")
        changed = replace(meeting, recorded_at_local="2026-11-09T20:01:00")
        assert changed.words is meeting.words
        index.sync(LibrarySnapshot(meetings=(changed,)))
        assert index.query("One", date="today", now_ms=today, tz="UTC")


@pytest.mark.parametrize("label", [None, "2026-11-09T00:01:00"])
def test_multi_term_title_date_filter_uses_same_d95_time_as_word_hits(tmp_path, label):
    meeting = replace(load_open_meeting(changed_file(tmp_path, local=label)), title="Supplier weekly meeting")
    with closing(SearchIndex(tmp_path / "Library", tmp_path / "cache")) as index:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        assert index.query("suppl meet", now_ms=START.timestamp() * 1000, tz="UTC")
        assert bool(index.query("suppl meet", date="today", now_ms=START.timestamp() * 1000, tz="UTC")) == (
            label is not None
        )


@pytest.mark.parametrize("number", ["005", "009", "015", "019", "020", "022"])
def test_bad_or_unknown_cassini_manifest_never_promotes_its_date_tag(tmp_path, number):
    from cassini_talk.received import import_received

    path = next((VECTORS / "opus").glob(number + "-*.opus"))
    document = portable.read_meeting(str(path))
    assert document.state in (portable.State.INVALID, portable.State.UNKNOWN_FORMAT)
    assert document.tags.get("DATE") and document.meeting_start is None
    assert load_open_meeting(path).display_start is None
    root = tmp_path / "Imported"
    assert import_received(path, root).display_start is None
    (rebuilt,) = LibraryScanner(root).scan().meetings
    assert rebuilt.display_start is None
    assert projection(rebuilt)["startMs"] is None


def test_pure_selector_distinguishes_unreadable_cassini_from_plain_audio():
    tags = {"DATE": "2026-11-09T09:40:48Z"}
    assert meeting_time.selected(None, tags) == {"source": "DATE", "value": tags["DATE"]}
    assert meeting_time.selected(None, dict(tags, cassini_format="org.cassini.portable-meeting/99")) is None


def rewrite_manifest(path, update):
    document = portable.read_meeting(str(path))
    manifest = deepcopy(document.manifest)
    update(manifest)
    tags = dict(document.tags)
    tags.pop("CASSINI_RECORDED_AT_UTC", None)
    tags["DATE"] = manifest["meeting"]["createdAtUtc"]
    tags.update(chunk_set("CASSINI_PAYLOAD_", manifest, MANIFEST_MIME)[0])
    staged = path.with_suffix(".partial")
    with path.open("rb") as incoming, staged.open("wb") as outgoing:
        rewrite_tags(incoming, outgoing, tags)
    staged.replace(path)


@pytest.mark.parametrize("label", [None, "2026-11-09T10:31:48"])
def test_own_legacy_publication_keeps_capture_instant_over_a_wall_label_and_reexports_it(tmp_path, label):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from library_v2_support import simple_session

    from cassini_talk.exporting import export_meeting
    from cassini_talk.library_v2.finalize import finalize

    root = tmp_path / "Original"
    root.mkdir()
    sid, _ = simple_session(root, 0.1, created=START.isoformat().replace("+00:00", "Z"))
    publication = finalize(root, sid, now=START + timedelta(minutes=9), zone="Europe/Rome")
    path = root / publication.name

    def legacy(manifest):
        manifest["meeting"].pop("recordedAtUtc")
        manifest["meeting"].pop("recordedAtLocal", None)
        if label:
            manifest["meeting"]["recordedAtLocal"] = label

    rewrite_manifest(path, legacy)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.published_file == path
    assert meeting.started_at_ms == int(START.timestamp() * 1000)
    exported = export_meeting(meeting, tmp_path / "Exports")
    result = portable.read_meeting(str(exported))
    assert result.recorded_at_ms == int(START.timestamp() * 1000)
    assert result.tags["DATE"] == result.tags["CASSINI_RECORDED_AT_UTC"] == result.manifest["meeting"]["recordedAtUtc"]


@pytest.mark.parametrize("origin", ["own", "received"])
def test_stale_audio_export_does_not_resign_any_recording_time(tmp_path, origin):
    from cassini_talk.exporting import export_meeting

    path = tmp_path / "Stale synthetic meeting.opus"
    shutil.copyfile(VECTORS / "opus/010-stale-audio.opus", path)

    def stamp(manifest):
        manifest["meeting"]["recordedAtUtc"] = "2026-11-09T09:31:48Z"
        manifest["meeting"]["recordedAtLocal"] = "2026-11-09T10:31:48"

    rewrite_manifest(path, stamp)
    embedded = portable.read_meeting(str(path))
    assert portable.verify_audio(str(path), embedded).state == portable.State.STALE_AUDIO
    model = replace(load_open_meeting(path), origin=origin, published_file=path)
    assert model.started_at_ms is not None
    exported = export_meeting(model, tmp_path / "Exports")
    result = portable.read_meeting(str(exported))
    assert "recordedAtUtc" not in result.manifest["meeting"]
    assert "recordedAtLocal" not in result.manifest["meeting"]
    assert "CASSINI_RECORDED_AT_UTC" not in result.tags
    assert result.tags["DATE"] == result.created_at and result.meeting_start is None


@pytest.mark.parametrize(
    "choice,day,expected",
    [
        ("today", (2026, 3, 8), True),
        ("today", (2026, 3, 7), False),
        ("week", (2026, 3, 14), True),
        ("month", (2026, 4, 6), True),
    ],
)
def test_label_calendar_bounds_do_not_move_with_midnight_dst(tmp_path, choice, day, expected):
    from zoneinfo import ZoneInfo

    model = replace(
        load_open_meeting(changed_file(tmp_path, local="2026-03-08T00:30:00")), title="Supplier weekly meeting"
    )
    zone = ZoneInfo("America/Havana")
    now = datetime(*day, 12, tzinfo=zone).timestamp() * 1000
    with closing(SearchIndex(tmp_path / "Library", tmp_path / "cache")) as index:
        index.sync(LibrarySnapshot(meetings=(model,)))
        for query in ("One", "suppl meet"):
            assert bool(index.query(query, date=choice, now_ms=now, tz=zone)) is expected


@pytest.mark.parametrize("number", ["027", "028", "029"])
def test_independent_pinned_reader_agrees_on_when(number):
    import runpy

    read_file = runpy.run_path(str(Path(__file__).parent / "export/cassini-read-pure.py"))["read_file"]
    path = next((VECTORS / "opus").glob(number + "-*.opus"))
    result, _ = read_file(path)
    ours = portable.read_meeting(str(path)).meeting_start
    assert (ours["source"], ours["value"]) == result["when"]
