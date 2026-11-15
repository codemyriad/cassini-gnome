# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 / W0.7: the shared import-types contract, safe picker MIME filters, Ogg
content validation, FLAC header validation (D102), and decoding MP3/M4A/WAV/FLAC
into Imported (Received) meetings."""

import configparser
import hashlib
import json
import re
import struct
import wave
from pathlib import Path

import pytest

from cassini_talk import audio_import, batch_import, import_feedback, import_types, received
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


def test_every_entry_is_decided_and_native_flac_is_offered():
    # D102 (cassini-ux import-types 2026-11-14.3): native FLAC is decided; no open entries remain.
    assert import_types.contract()["version"] == "2026-11-14.3"
    assert import_types.extensions() == (".opus", ".ogg", ".mp3", ".m4a", ".wav", ".flac")
    assert import_types.labels() == ("Opus/Ogg", "MP3", "M4A/AAC", "WAV", "FLAC")
    assert [e["id"] for e in import_types.contract()["types"] if e["status"] != "decided"] == []
    assert {"audio/flac", "audio/x-flac"} <= set(import_types.picker_mime_types())
    assert ".flac" not in import_types.rejected_extensions()
    assert import_types.rejected_extensions() == (".ogv", ".oga", ".ogx", ".spx")
    assert import_types.is_plain_audio("Song.FLAC")
    rule = import_types.header_validation("song.flac")
    assert rule["minProbeBytes"] == 42 and rule["magicHex"] == "664c6143"
    assert import_types.header_validation("song.ogg") is None


def test_picker_uses_safe_mime_types_only_never_compatibility_or_video():
    mimes = import_types.picker_mime_types()
    assert mimes == tuple(dict.fromkeys(m for e in import_types.decided() for m in e["mimeTypes"]))
    compat = {m for e in import_types.contract()["types"] for m in e["compatibilityMimeTypes"]}
    assert compat >= {"audio/ogg", "application/ogg"}
    assert compat.isdisjoint(mimes)
    assert all(m.startswith("audio/") for m in mimes)
    for ogg_flac in ("video/ogg", "audio/x-flac+ogg", "audio/x-oggflac", "audio/ogg", "application/ogg"):
        assert ogg_flac not in mimes


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


class _FakeFileDialog:
    """Captures what on_open hands to Gtk.FileDialog without opening a window."""

    last = None

    def __init__(self, title):
        self.title = title
        self.filters = None
        self.default = None
        self.opened = False
        _FakeFileDialog.last = self

    def set_filters(self, filters):
        self.filters = [filters.get_item(i) for i in range(filters.get_n_items())]

    def set_default_filter(self, default):
        self.default = default

    def open_multiple(self, *_args):
        self.opened = True


class _FakeApp:
    def get_active_window(self):
        return None

    def _on_open_chosen(self, *_args):
        pass


@pytest.mark.parametrize("lang", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_open_dialog_offers_one_catalogued_filter_naming_audio(monkeypatch, lang):
    # D-1061: the default filter admits audio, so its name says so, in every UI
    # language; no uncatalogued developer-only "Recording Session" filter.
    from cassini_talk import main
    from cassini_talk.strings import CATALOG, ENTRIES

    monkeypatch.setattr(main.Gtk, "FileDialog", _FakeFileDialog)
    with using_language(lang):
        main.CassiniApplication.on_open(_FakeApp())
        expected = text("import.filter.supported")
        localized = {entry.get("l10n", {}).get(lang, {}).get("text", entry["en"]) for entry in ENTRIES.values()}
    dialog = _FakeFileDialog.last
    assert dialog.opened
    assert len(dialog.filters) == 1 and dialog.default is dialog.filters[0]
    assert dialog.filters[0].get_name() == expected
    if lang == "en":
        assert expected == CATALOG["import.filter.supported"] == "Audio, Cassini files and ZIP archives"
    else:
        assert expected != CATALOG["import.filter.supported"]
    assert dialog.title in localized
    assert all(f.get_name() in localized for f in dialog.filters)


def test_open_dialog_source_has_no_session_filter():
    source = (ROOT / "src/cassini_talk/main.py").read_text()
    body = source[source.index("    def on_open(") : source.index("    def _on_open_chosen(")]
    assert "Recording Session" not in body and "add_pattern" not in body
    assert 'text("import.filter.supported")' in body and "cassiniAndZip" not in body


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
        ("song.flac", None, True),
        ("Song.FLAC", "audio/flac", True),
        ("song.flac", "audio/x-flac", True),
        # D102: Ogg FLAC and broad Ogg MIME types never pass for .flac ...
        ("song.flac", "audio/x-flac+ogg", False),
        ("song.flac", "audio/x-oggflac", False),
        ("song.flac", "audio/ogg", False),
        ("song.flac", "application/ogg", False),
        ("song.flac", "audio/mpeg", False),
        ("song.flac", "video/x-flac", False),
        # ... nor Ogg-FLAC MIME types on the Ogg extensions.
        ("song.ogg", "audio/x-flac+ogg", False),
        ("song.ogg", "audio/x-oggflac", False),
        ("song.opus", "audio/x-flac+ogg", False),
        ("song.opus", "audio/x-oggflac", False),
        ("song.oga", "audio/x-flac+ogg", False),
        ("song.oga", "audio/x-oggflac", False),
        ("song.oga", "audio/ogg", False),
        ("song.oga", None, False),
        # Broad Ogg MIME types are compatibility types for Opus/Ogg only.
        ("song.ogg", "audio/ogg", True),
        ("song.opus", "application/ogg", True),
        ("talk.mp3", "audio/ogg", False),
        ("talk.wav", "application/ogg", False),
        ("talk.m4a", "audio/flac", False),
        ("speech.spx", None, False),
        ("clip.ogg", "video/ogg", False),
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


# ---- native FLAC header validation (D102) ----------------------------------------


def flac_header(*, magic=b"fLaC", block=0x00, length=34, rate=48000, channels=1, bits=16, samples=48000):
    """``fLaC`` + metadata block header + 34-byte STREAMINFO (RFC 9639 section 8.2)."""
    info = struct.pack(">HH", 4096, 4096) + bytes(3) + bytes(3)
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | samples
    info += packed.to_bytes(8, "big") + bytes(16)
    assert len(info) == 34
    return magic + bytes([block]) + length.to_bytes(3, "big") + info


FLAC_RULE = import_types.header_validation("a.flac")
VALID_FLAC = flac_header()


@pytest.mark.parametrize(
    "data, reason",
    [
        (VALID_FLAC, None),
        # STREAMINFO flagged as the last metadata block (high bit) is still type 0.
        (flac_header(block=0x80), None),
        (VALID_FLAC + bytes(4096), None),
        (flac_header(rate=8000, channels=2, bits=24), None),
        (flac_header(rate=0xFFFFF), None),
        # Truncated: fewer than the 42 header bytes.
        (VALID_FLAC[:41], "truncated"),
        (VALID_FLAC[:8], "truncated"),
        (b"fLaC", "truncated"),
        (b"", "not-flac"),
        # Corrupt STREAMINFO block header or sample rate.
        (flac_header(block=0x04), "not-streaminfo"),
        (flac_header(block=0x84), "not-streaminfo"),
        (flac_header(length=33), "streaminfo-length"),
        (flac_header(length=0x010022), "streaminfo-length"),
        (flac_header(rate=0), "sample-rate"),
        (flac_header(rate=0, channels=8, bits=32, samples=(1 << 36) - 1), "sample-rate"),
        # Other containers renamed to .flac.
        (ogg_page(b"\x7fFLAC\x01\x00\x00\x01" + VALID_FLAC, bos=True) + bytes(64), "ogg"),
        (OPUS_STREAM, "ogg"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00" + VALID_FLAC, "id3"),
        (b"RIFF" + bytes(60), "not-flac"),
        (b"flac" + VALID_FLAC[4:], "not-flac"),
        ((FIXTURES / "tone.mp3").read_bytes()[:42], "id3"),
    ],
)
def test_flac_header_validation(data, reason):
    assert import_types.check_header_bytes(data, FLAC_RULE) == reason


def test_real_flac_fixture_passes_the_header_check_and_probe_reads_only_42_bytes(tmp_path):
    data = (FIXTURES / "tone.flac").read_bytes()
    assert import_types.check_header_bytes(data[:42], FLAC_RULE) is None
    with (FIXTURES / "tone.flac").open("rb") as stream:
        stream.seek(9)
        assert import_types.check_header_file(stream, FLAC_RULE) is None
        assert stream.tell() == 9


def _bad_flac_files(tmp_path):
    files = {
        "truncated": VALID_FLAC[:30],
        "zero-rate": flac_header(rate=0) + bytes(512),
        "not-streaminfo": flac_header(block=0x04) + bytes(512),
        "ogg-flac": ogg_page(b"\x7fFLAC\x01\x00\x00\x01" + VALID_FLAC, bos=True) + bytes(512),
        "id3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + (FIXTURES / "tone.flac").read_bytes(),
        "empty": b"",
    }
    paths = []
    for name, data in files.items():
        path = tmp_path / f"{name}.flac"
        path.write_bytes(data)
        paths.append(path)
    return paths


def test_invalid_flac_headers_fail_before_decoding_and_publish_nothing(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    decoded = []
    monkeypatch.setattr(received, "decode_to_opus", lambda *a, **k: decoded.append(a))
    paths = _bad_flac_files(tmp_path)
    for path in paths:
        with pytest.raises(ValueError, match="native FLAC"):
            received.import_one(path, root)
    result = batch_import.import_files(paths, root)
    assert (result.imported, result.failed, result.ignored) == (0, len(paths), 0)
    assert result.failed_inputs == [str(p) for p in paths]
    assert decoded == [] and documents(root) == [] and not list(root.glob(".received-*"))


def test_native_flac_disguised_as_ogg_or_opus_is_rejected(tmp_path):
    root = tmp_path / "Library"
    flac = (FIXTURES / "tone.flac").read_bytes()
    assert import_types.check_ogg_bytes(flac[:1024]) == "not-ogg"
    ogg = tmp_path / "song.ogg"
    ogg.write_bytes(flac)
    with pytest.raises(ValueError):
        received.import_one(ogg, root)
    assert documents(root) == []


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
    if _gst_decodes("audio/x-flac"):
        sources["flac"] = FIXTURES / "tone.flac"
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


needs_flac = pytest.mark.skipif(not _gst_decodes("audio/x-flac"), reason="GStreamer FLAC decoder unavailable")


@needs_encoder
@needs_flac
def test_native_flac_decodes_into_an_imported_opus_meeting_in_the_library(tmp_path):
    source = tmp_path / "Quarterly review.flac"
    source.write_bytes((FIXTURES / "tone.flac").read_bytes())
    root = tmp_path / "Library"
    result = batch_import.import_files([source], root)
    assert (result.imported, result.failed, result.ignored) == (1, 0, 0)
    [directory] = documents(root)
    value = json.loads((directory / "meeting.json").read_text())["received"]
    assert value["originalName"] == "Quarterly review.opus"
    assert value["sourceSha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    copy = directory / value["originalName"]
    assert import_types.check_ogg_bytes(copy.read_bytes()[:1024]) is None
    [meeting] = LibraryScanner(root).scan().meetings
    assert meeting.directory == directory and meeting.origin == "received" and meeting.has_audio
    assert meeting.badge().text == text("states.badge.received")
    # About one second of audio survived the FLAC -> Opus decode.
    assert 800 <= meeting.duration_ms <= 1200
    assert result.last_key == meeting.key


def test_flac_streaminfo_reads_the_rate_and_the_36_bit_total_sample_count():
    assert import_types.flac_streaminfo((FIXTURES / "tone.flac").read_bytes()[:42]) == (48000, 48000)
    assert import_types.flac_streaminfo(flac_header(rate=44100, samples=0)) == (44100, 0)
    top = flac_header(rate=0xFFFFF, channels=8, bits=32, samples=(1 << 36) - 1)
    assert import_types.flac_streaminfo(top) == (0xFFFFF, (1 << 36) - 1)
    assert import_types.flac_streaminfo(flac_header(samples=1 << 32)) == (48000, 1 << 32)


def _flac_crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_frame_offsets(data):
    """Frame offsets of ``tone.flac``, each sync code confirmed by the header CRC-8.

    RFC 9639 section 9.1: sync + block size/rate codes + channels/bits, a
    UTF-8 coded frame number, optional uncommon block size and sample rate.
    """
    offsets = []
    for at in range(42, len(data) - 16):
        if data[at] != 0xFF or data[at + 1] != 0xF8:
            continue
        first = data[at + 4]
        number = 1 if first < 0x80 else 8 - (~first & 0xFF).bit_length()
        end = at + 4 + number
        end += {6: 1, 7: 2}.get(data[at + 2] >> 4, 0)
        end += {12: 1, 13: 2, 14: 2}.get(data[at + 2] & 0x0F, 0)
        if _flac_crc8(data[at:end]) == data[end]:
            offsets.append(at)
    return offsets


def _flac_variant(tmp_path, name, *, cut=None, streamed=False):
    data = bytearray((FIXTURES / "tone.flac").read_bytes())
    if streamed:
        data[21] &= 0xF0  # STREAMINFO total samples = 0: unknown length.
        data[22:26] = bytes(4)
    path = tmp_path / name
    path.write_bytes(bytes(data[:cut]))
    return path


def test_tone_flac_frame_offsets_are_found():
    data = (FIXTURES / "tone.flac").read_bytes()
    offsets = _flac_frame_offsets(data)
    # One second at 48 kHz in 4608-sample blocks: ten full frames and a short last one.
    assert len(offsets) == 11 and offsets[0] > 42 and offsets[-1] < len(data)
    assert offsets[5] < len(data) // 2 < offsets[6]


def _assert_flac_rejected(path, root):
    with pytest.raises(ValueError, match="incomplete"):
        received.import_one(path, root)
    assert documents(root) == [] and not list(root.glob(".received-*"))
    result = batch_import.import_files([path], root)
    assert (result.imported, result.failed) == (0, 1)
    # A failed input becomes a Needs attention failure card in the Library.
    assert result.failed_inputs == [str(path)]
    assert import_entry_badge({"failed": True}).kind == "needsAttention"
    assert documents(root) == [] and not list(root.glob(".received-*"))


@needs_encoder
@needs_flac
def test_flac_cut_mid_frame_is_rejected_and_publishes_nothing(tmp_path):
    # Half of the file: the decoder silently drops the cut-off frame.
    _assert_flac_rejected(_flac_variant(tmp_path, "half.flac", cut=10966), tmp_path / "Library")


@needs_encoder
@needs_flac
@pytest.mark.parametrize("frames_missing", [1, 2, 6])
def test_flac_cut_on_a_frame_boundary_is_rejected_by_the_streaminfo_total(tmp_path, frames_missing):
    # The decoder reaches a clean EOS: only STREAMINFO's total sample count
    # shows that whole trailing frames are missing.
    offsets = _flac_frame_offsets((FIXTURES / "tone.flac").read_bytes())
    path = _flac_variant(tmp_path, "boundary.flac", cut=offsets[-frames_missing])
    _assert_flac_rejected(path, tmp_path / "Library")


@needs_encoder
@needs_flac
@pytest.mark.parametrize("cut", [10966, -1, -7])
def test_streamed_flac_with_a_partial_last_frame_is_rejected(tmp_path, cut):
    # STREAMINFO total = 0: every byte of the file must belong to a parsed frame.
    path = _flac_variant(tmp_path, "streamed.flac", cut=cut, streamed=True)
    _assert_flac_rejected(path, tmp_path / "Library")


@needs_encoder
@needs_flac
def test_complete_streamed_flac_still_imports(tmp_path):
    path = _flac_variant(tmp_path, "streamed.flac", streamed=True)
    root = tmp_path / "Library"
    meeting, added = received.import_one(path, root)
    assert added and meeting.has_audio and 800 <= meeting.duration_ms <= 1200
    assert documents(root) == [meeting.directory] and not list(root.glob(".received-*"))


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


def _truncate_wav(path, keep_seconds, rate=16000):
    """Keep the header (declaring the full length) but only ``keep_seconds`` of samples."""
    data = path.read_bytes()
    start = data.index(b"data") + 8
    path.write_bytes(data[: start + int(keep_seconds * rate) * 2])
    return path


def _mpeg_frame(tag=None, total=0):
    # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, stereo: 417-byte frames.
    frame = bytearray(b"\xff\xfb\x90\x00" + bytes(413))
    if tag is not None:
        # Xing/Info follows 32 bytes of stereo MPEG-1 side information.
        frame[36:52] = tag + struct.pack(">III", 3, 10, total)
    return bytes(frame)


def test_complete_containers_pass_the_truncation_check(tmp_path):
    audio_import.check_complete(write_wav(tmp_path / "ok.wav"))
    audio_import.check_complete(FIXTURES / "tone.m4a")
    audio_import.check_complete(FIXTURES / "tone.mp3")
    mp3 = tmp_path / "info.mp3"
    mp3.write_bytes(_mpeg_frame(b"Info", 417 * 10) + _mpeg_frame() * 9)
    audio_import.check_complete(mp3)
    # A streamed WAV leaves its data length unknown; that is not truncation.
    streamed = write_wav(tmp_path / "streamed.wav")
    data = bytearray(streamed.read_bytes())
    at = data.index(b"data") + 4
    data[4:8] = data[at : at + 4] = struct.pack("<I", 0xFFFFFFFF)
    streamed.write_bytes(data)
    audio_import.check_complete(streamed)


def test_truncated_containers_are_rejected(tmp_path):
    wav = _truncate_wav(write_wav(tmp_path / "cut.wav", seconds=10), 1)
    header_only = tmp_path / "header.wav"
    header_only.write_bytes(write_wav(tmp_path / "h.wav").read_bytes()[:20])
    m4a = FIXTURES / "tone.m4a"
    whole = m4a.read_bytes()
    in_mdat = tmp_path / "mdat.m4a"
    in_mdat.write_bytes(whole[:2000])
    in_moov = tmp_path / "moov.m4a"
    in_moov.write_bytes(whole[:-100])
    no_moov = tmp_path / "nomoov.m4a"
    no_moov.write_bytes(whole[: whole.index(b"moov") - 4])
    id3 = tmp_path / "id3.mp3"
    id3.write_bytes((FIXTURES / "tone.mp3").read_bytes()[:12])
    xing = tmp_path / "xing.mp3"
    xing.write_bytes(_mpeg_frame(b"Xing", 417 * 10) + _mpeg_frame() * 3)
    for path in (wav, header_only, in_mdat, in_moov, no_moov, id3, xing):
        with pytest.raises(ValueError, match="incomplete"):
            audio_import.check_complete(path)


@needs_encoder
def test_truncated_wav_fails_without_publishing_and_leaves_no_staging(tmp_path):
    # The header declares 10 s; only 1 s of samples is on disk.
    source = _truncate_wav(write_wav(tmp_path / "cut.wav", seconds=10), 1)
    root = tmp_path / "Library"
    with pytest.raises(ValueError, match="incomplete"):
        received.import_one(source, root)
    result = batch_import.import_files([source], root)
    assert result.failed == 1 and result.imported == 0
    assert documents(root) == [] and not list(root.glob(".received-*"))


@needs_encoder
def test_truncated_m4a_fails_without_publishing_and_leaves_no_staging(tmp_path):
    source = tmp_path / "cut.m4a"
    source.write_bytes((FIXTURES / "tone.m4a").read_bytes()[:2000])
    root = tmp_path / "Library"
    with pytest.raises(ValueError, match="incomplete"):
        received.import_one(source, root)
    assert documents(root) == [] and not list(root.glob(".received-*"))


@needs_encoder
def test_decoded_opus_is_fsynced_and_a_sync_failure_publishes_nothing(tmp_path, monkeypatch):
    import errno
    import os

    source = write_wav(tmp_path / "talk.wav")
    root = tmp_path / "Library"
    real_fsync = os.fsync
    synced = []

    def fsync(fd):
        name = os.readlink(f"/proc/self/fd/{fd}")
        if name.endswith(".opus"):
            synced.append(name)
            raise OSError(errno.EIO, "simulated I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError):
        received.import_one(source, root)
    assert synced and "/.received-" in synced[0]
    assert documents(root) == [] and not list(root.glob(".received-*"))
    result = batch_import.import_files([source], root)
    assert result.failed == 1 and result.imported == 0
    assert documents(root) == [] and not list(root.glob(".received-*"))

    # With a working disk, the Opus copy is synced before the meeting appears.
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.readlink(f"/proc/self/fd/{fd}")), real_fsync(fd))[1])
    synced.clear()
    meeting, added = received.import_one(source, root)
    assert added and any(name.endswith(".opus") for name in synced)


def test_ogg_video_and_ogg_flac_named_ogg_are_rejected_before_copying(tmp_path):
    root = tmp_path / "Library"
    video = tmp_path / "clip.ogg"
    video.write_bytes(ogg_page(b"\x80theora" + bytes(40), bos=True) + bytes(2000))
    flac = tmp_path / "song.ogg"
    flac.write_bytes(ogg_page(b"\x7fFLAC" + bytes(40), bos=True) + bytes(2000))
    for path in (video, flac):
        with pytest.raises(ValueError):
            received.import_one(path, root)
    result = batch_import.import_files([video, flac, tmp_path / "x.ogv", tmp_path / "y.oga"], root)
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
        assert import_feedback.accepted_formats() == "Opus/Ogg, MP3, M4A/AAC, WAV and FLAC"
        assert import_feedback.cant_import_detail() == "Cassini imports Opus/Ogg, MP3, M4A/AAC, WAV and FLAC."


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
        ("a.flac", True),
        ("a.FLAC", True),
        ("a.oga", False),
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
