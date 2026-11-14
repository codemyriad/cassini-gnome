# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 / W0.7: the shared import-types contract, safe picker MIME filters, Ogg
content validation, and decoding MP3/M4A/WAV into Imported (Received) meetings."""

import configparser
import hashlib
import json
import re
import struct
import wave
from pathlib import Path

import pytest

from cassini_talk import batch_import, import_feedback, import_types, received
from cassini_talk.i18n import using_language
from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_state import import_entry_badge
from cassini_talk.strings import text

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests/ux/UPSTREAM.json"
FIXTURES = Path(__file__).parent / "fixtures/import-audio"
OPUS = Path(__file__).parent / "conformance/opus/002-plain-audio.opus"


# ---- pin -------------------------------------------------------------------


def test_contract_is_vendored_whole_at_the_pinned_commit():
    pins = json.loads(UPSTREAM.read_text())
    assert pins["paths"]["import-types.json"] == "src/cassini_talk/import-types.json"
    assert pins["sourcePaths"]["import-types.json"] == "contract/import-types.json"
    data = import_types.CONTRACT.read_bytes()
    assert hashlib.sha256(data).hexdigest() == pins["sha256"]["import-types.json"]
    # One merged cassini-ux commit for every vendored file, brand included.
    brand = json.loads((ROOT / "ux/brand/UPSTREAM.json").read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", pins["commit"]) and brand["commit"] == pins["commit"]
    assert import_types.contract()["schema"] == "cassini-ux/import-types@1"


def test_contract_file_is_installed_with_the_module():
    sources = (ROOT / "src/meson.build").read_text()
    for name in ("import_types.py", "import-types.json", "audio_import.py"):
        assert f"'cassini_talk/{name}'" in sources


# ---- decided list and picker filters ------------------------------------------


def test_only_decided_entries_are_offered_and_flac_stays_out():
    assert import_types.extensions() == (".opus", ".ogg", ".mp3", ".m4a", ".wav")
    assert import_types.labels() == ("Opus/Ogg", "MP3", "M4A/AAC", "WAV")
    open_entries = [e for e in import_types.contract()["types"] if e["status"] != "decided"]
    assert [e["id"] for e in open_entries] == ["flac"]
    offered = set(import_types.picker_mime_types())
    for entry in open_entries:
        assert offered.isdisjoint(entry["mimeTypes"])
        assert not set(entry["extensions"]) & set(import_types.extensions())


def test_picker_uses_safe_mime_types_only_never_compatibility_or_video():
    mimes = import_types.picker_mime_types()
    assert mimes == tuple(dict.fromkeys(m for e in import_types.decided() for m in e["mimeTypes"]))
    compat = {m for e in import_types.contract()["types"] for m in e["compatibilityMimeTypes"]}
    assert compat >= {"audio/ogg", "application/ogg"}
    assert compat.isdisjoint(mimes)
    assert all(m.startswith("audio/") for m in mimes)
    assert "video/ogg" not in mimes and "audio/x-flac+ogg" not in mimes


def test_gtk_open_dialog_and_open_with_declarations_follow_the_contract():
    source = (ROOT / "src/cassini_talk/main.py").read_text()
    body = source[source.index("    def on_open(") : source.index("    def _on_open_chosen(")]
    assert "picker_mime_types()" in body
    # GtkFileFilter subclass matching: no broad Ogg MIME and no audio suffix globs.
    for broad in ('"audio/ogg"', '"application/ogg"', 'add_suffix("ogg")', 'add_suffix("opus")'):
        assert broad not in body
    desktop = configparser.RawConfigParser(strict=False)
    desktop.read_string((ROOT / "data/io.codemyriad.CassiniTalk.desktop.in").read_text())
    declared = tuple(m for m in desktop["Desktop Entry"]["MimeType"].split(";") if m)
    assert declared == import_types.picker_mime_types()
    metainfo = (ROOT / "data/io.codemyriad.CassiniTalk.metainfo.xml.in").read_text()
    assert tuple(re.findall(r"<mediatype>([^<]+)</mediatype>", metainfo)) == import_types.picker_mime_types()


@pytest.mark.parametrize(
    "name, mime, ok",
    [
        ("meeting.opus", None, True),
        ("Meeting.OGG", None, True),
        ("talk.mp3", "audio/mpeg", True),
        ("talk.m4a", "audio/mp4", True),
        ("talk.wav", "audio/x-wav", True),
        ("clip.ogv", None, False),
        ("song.oga", None, False),
        ("song.flac", None, False),
        ("speech.spx", None, False),
        ("clip.ogg", "video/ogg", False),
        ("song.ogg", "audio/x-flac+ogg", False),
        ("movie.mp4", None, False),
        ("raw.aac", None, False),
        ("noext", None, False),
    ],
)
def test_name_and_mime_acceptance(name, mime, ok):
    assert import_types.acceptable_name(name, mime) is ok


def test_ambiguous_ogg_and_compatibility_mime_need_a_content_probe():
    assert import_types.needs_ogg_probe("a.ogg")
    assert import_types.needs_ogg_probe("a.opus", "application/ogg")
    assert import_types.needs_ogg_probe("a.opus", "audio/ogg; codecs=opus")
    assert not import_types.needs_ogg_probe("a.opus")
    assert not import_types.needs_ogg_probe("a.opus", "audio/x-opus+ogg")


# ---- Ogg content validation -----------------------------------------------------


def ogg_page(payload, *, bos=False, serial=1, sequence=0):
    lacing = bytearray()
    remaining = len(payload)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    header = b"OggS" + bytes([0, 0x02 if bos else 0]) + struct.pack("<qIII", 0, serial, sequence, 0)
    return header + bytes([len(lacing)]) + bytes(lacing) + payload


OPUS_HEAD = b"OpusHead" + bytes([1, 1]) + struct.pack("<HIhB", 312, 48000, 0, 0)
OPUS_TAGS = b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0)
OPUS_STREAM = ogg_page(OPUS_HEAD, bos=True) + ogg_page(OPUS_TAGS, sequence=1) + bytes(2000)


@pytest.mark.parametrize(
    "data, reason",
    [
        (OPUS_STREAM, None),
        (OPUS_STREAM[:1024], None),
        (ogg_page(b"\x80theora" + bytes(40), bos=True) + ogg_page(b"\x81theora", sequence=1), "rejected-stream"),
        (ogg_page(b"\x7fFLAC" + bytes(40), bos=True) + ogg_page(b"\x84", sequence=1), "rejected-stream"),
        (ogg_page(b"\x01vorbis" + bytes(30), bos=True) + ogg_page(b"\x03vorbis", sequence=1), "rejected-stream"),
        (ogg_page(b"fishead\x00" + bytes(40), bos=True), "rejected-stream"),
        (ogg_page(b"Speex   " + bytes(40), bos=True), "rejected-stream"),
        # Opus first, Theora multiplexed as a second BOS page (Ogg video with Opus audio).
        (ogg_page(OPUS_HEAD, bos=True) + ogg_page(b"\x80theora" + bytes(40), bos=True, serial=2), "multiplexed"),
        (ogg_page(OPUS_HEAD, bos=True) + ogg_page(b"NotTags!", sequence=1), "not-opus"),
        (ogg_page(OPUS_HEAD, bos=True), "not-opus"),
        (ogg_page(OPUS_HEAD, bos=False) + ogg_page(OPUS_TAGS, sequence=1), "not-ogg"),
        (b"RIFF" + bytes(100), "not-ogg"),
        (b"", "not-ogg"),
        (OPUS_STREAM[:20], "not-ogg"),
    ],
)
def test_ogg_probe_accepts_only_ogg_opus(data, reason):
    assert import_types.check_ogg_bytes(data) == reason


def test_ogg_probe_reads_only_the_contract_probe_window(tmp_path):
    path = tmp_path / "a.ogg"
    path.write_bytes(OPUS_STREAM)

    class Counting:
        def __init__(self, stream):
            self.stream, self.reads = stream, []

        def read(self, n):
            self.reads.append(n)
            return self.stream.read(n)

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

    with path.open("rb") as stream:
        counting = Counting(stream)
        counting.seek(7)
        assert import_types.check_ogg_file(counting) is None
        assert counting.reads == [import_types.probe_bytes()] == [1024]
        assert counting.tell() == 7


# ---- import into the Library ------------------------------------------------------


def _gst_has(*names):
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except (ImportError, ValueError):
        return False
    return all(Gst.ElementFactory.find(name) for name in names)


def _gst_decodes(caps):
    if not _gst_has("decodebin"):
        return False
    from gi.repository import Gst

    wanted = Gst.Caps.from_string(caps)
    for factory in Gst.ElementFactory.list_get_elements(Gst.ELEMENT_FACTORY_TYPE_DECODER, Gst.Rank.MARGINAL):
        for template in factory.get_static_pad_templates():
            if template.direction == Gst.PadDirection.SINK and template.get_caps().can_intersect(wanted):
                return True
    return False


ENCODE = ("decodebin", "audioconvert", "audioresample", "opusenc", "oggmux", "filesrc", "filesink")
needs_encoder = pytest.mark.skipif(not _gst_has(*ENCODE), reason="GStreamer Opus encode path unavailable")


def write_wav(path, seconds=1.0, rate=16000):
    import math

    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        frames = int(seconds * rate)
        stream.writeframes(
            b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames))
        )
    return path


def documents(root):
    return sorted(root.glob("Received-*"))


def _plain_sources(tmp_path):
    sources = {"wav": write_wav(tmp_path / "Weekly sync.wav")}
    if _gst_decodes("audio/mpeg,mpegversion=1,layer=3"):
        sources["mp3"] = FIXTURES / "tone.mp3"
    if _gst_decodes("audio/mpeg,mpegversion=4,stream-format=raw"):
        sources["m4a"] = FIXTURES / "tone.m4a"
    return sources


@needs_encoder
def test_plain_audio_imports_decode_into_imported_opus_meetings_and_keep_the_original(tmp_path):
    sources = _plain_sources(tmp_path)
    before = {kind: path.read_bytes() for kind, path in sources.items()}
    root = tmp_path / "Library"
    for kind, path in sources.items():
        meeting, added = received.import_one(path, root)
        assert added
        assert meeting.origin == "received" and meeting.has_audio
        assert meeting.lifecycle == "saved" and meeting.transcript == "none"
        # Imported provenance badge (L-57 copy for origin = received).
        assert meeting.badge().text == text("states.badge.received")
        value = json.loads((meeting.directory / "meeting.json").read_text())["received"]
        copy = meeting.directory / value["originalName"]
        assert value["originalName"] == path.stem + ".opus"
        assert value["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
        assert value["sourceSha256"] == hashlib.sha256(before[kind]).hexdigest()
        assert import_types.check_ogg_bytes(copy.read_bytes()[:1024]) is None
        assert path.read_bytes() == before[kind], "the original file is never modified"
    assert len(documents(root)) == len(sources)
    assert not list(root.glob(".received-*"))
    assert {m.directory for m in LibraryScanner(root).scan().meetings} == set(documents(root))


@needs_encoder
def test_reimporting_the_same_plain_audio_deduplicates_on_the_original_digest(tmp_path, monkeypatch):
    source = write_wav(tmp_path / "talk.wav")
    root = tmp_path / "Library"
    first, added = received.import_one(source, root)
    assert added
    calls = []
    monkeypatch.setattr(received, "decode_to_opus", lambda *a, **k: calls.append(a))
    again, added = received.import_one(source, root)
    assert not added and again.directory == first.directory and calls == []
    # A batch with the same file twice (and the one already imported) decodes nothing new.
    result = batch_import.import_files([source, source], root)
    assert (result.imported, result.duplicates, result.failed) == (0, 2, 0)
    assert len(documents(root)) == 1


@needs_encoder
def test_damaged_plain_audio_fails_without_publishing_and_leaves_no_staging(tmp_path):
    root = tmp_path / "Library"
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"ID3" + bytes(64) + b"definitely not mpeg audio" * 40)
    with pytest.raises(ValueError):
        received.import_one(bad, root)
    result = batch_import.import_files([bad], root)
    assert result.failed == 1 and result.failed_inputs == [str(bad)]
    assert documents(root) == [] and not list(root.glob(".received-*"))


def test_ogg_video_and_ogg_flac_named_ogg_are_rejected_before_copying(tmp_path):
    root = tmp_path / "Library"
    video = tmp_path / "clip.ogg"
    video.write_bytes(ogg_page(b"\x80theora" + bytes(40), bos=True) + bytes(2000))
    flac = tmp_path / "song.ogg"
    flac.write_bytes(ogg_page(b"\x7fFLAC" + bytes(40), bos=True) + bytes(2000))
    for path in (video, flac):
        with pytest.raises(ValueError):
            received.import_one(path, root)
    result = batch_import.import_files([video, flac, tmp_path / "x.ogv", tmp_path / "y.flac"], root)
    assert (result.imported, result.failed, result.ignored) == (0, 2, 2)
    assert result.failed_inputs == [str(video), str(flac)]
    assert documents(root) == []


def test_ogg_opus_named_ogg_still_imports_byte_for_byte(tmp_path):
    root = tmp_path / "Library"
    source = tmp_path / "Planning.ogg"
    source.write_bytes(OPUS.read_bytes())
    meeting, added = received.import_one(source, root)
    assert added
    value = json.loads((meeting.directory / "meeting.json").read_text())["received"]
    assert (meeting.directory / value["originalName"]).read_bytes() == OPUS.read_bytes()
    assert "sourceSha256" not in value


def test_batch_import_takes_decided_types_and_ignores_the_rest(tmp_path):
    assert frozenset(import_types.extensions()) == batch_import.SUPPORTED


# ---- drop states and list cards (catalog strings) ----------------------------------


@pytest.mark.parametrize("locale", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_drop_error_names_the_file_and_the_contract_formats(locale):
    with using_language(locale):
        formats = import_feedback.accepted_formats()
        for label in import_types.labels():
            assert label in formats
        assert import_feedback.cant_import_detail() == text("import.drop.cantImport.detail", formats=formats)
    with using_language("en"):
        assert import_feedback.accepted_formats() == "Opus/Ogg, MP3, M4A/AAC and WAV"
        assert import_feedback.cant_import_detail() == "Cassini imports Opus/Ogg, MP3, M4A/AAC and WAV."


@pytest.mark.parametrize(
    "name, ok",
    [
        ("a.opus", True),
        ("a.ogg", True),
        ("a.MP3", True),
        ("a.m4a", True),
        ("a.wav", True),
        ("Notes.ZIP", True),
        ("a.ogv", False),
        ("a.flac", False),
        ("a.txt", False),
    ],
)
def test_droppable_names_follow_the_contract(name, ok):
    assert import_feedback.droppable_name(name) is ok


def test_failed_import_card_keeps_needs_attention_and_importing_stays_neutral():
    assert import_entry_badge({"failed": True}).as_dict() == {
        "kind": "needsAttention",
        "text": text("states.badge.needsAttention"),
        "tint": "amber",
        "glyph": "alert",
    }
    assert import_entry_badge({"importing": True, "failed": True}).kind == "importing"
    assert import_entry_badge({"importing": True}).tint == "neutral"
    assert import_entry_badge({"failed": False}) is None
