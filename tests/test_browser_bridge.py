# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import io
import struct
import wave
from pathlib import Path

import pytest

from cassini_viewer.recording_session import MAX_MESSAGE, BrowserHost, load_session, read_message, write_message


def track(host, identifier="source", **changes):
    message = dict(type="track", id=identifier, label="Test", kind="remote", sampleRate=48000, channels=1)
    message.update(changes)
    return host.handle(message)


def audio(host, identifier="source", offset=100, sequence=0, data=None):
    return host.handle(
        dict(
            type="audio",
            id=identifier,
            offsetMs=offset,
            sequence=sequence,
            pcm=base64.b64encode(data or b"\x01\x00" * 480).decode(),
        )
    )


@pytest.fixture
def host(tmp_path):
    instance = BrowserHost(tmp_path)
    instance.handle({"type": "start"})
    yield instance
    instance.close()


def test_protocol_round_trip_and_partial_reads():
    class Partial(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 1))

    output = io.BytesIO()
    write_message(output, {"type": "start"})
    incoming = Partial(output.getvalue())
    assert read_message(incoming) == {"type": "start"}
    assert read_message(incoming) is None


@pytest.mark.parametrize(
    "data", [b"\x01", struct.pack("=I", MAX_MESSAGE + 1), struct.pack("=I", 12) + b"{}", struct.pack("=I", 2) + b"[]"]
)
def test_protocol_rejects_invalid_frames(data):
    with pytest.raises(ValueError):
        read_message(io.BytesIO(data))


def test_gaps_offsets_and_header_recovery(host):
    track(host)
    audio(host)
    audio(host, offset=150, sequence=1)
    writer = host.session.writers["source"]
    # Header is valid even before stop or disconnect.
    file = host.session.directory / host.session.manifest["tracks"][0]["file"]
    with wave.open(str(file)) as wav:
        assert wav.getnframes() == 2880
        data = wav.readframes(2880)
        assert data[960:4800] == bytes(3840)
    host.close("Simulated disconnect")
    manifest = load_session(host.session.directory / "session.json")
    assert manifest["state"] == "interrupted"
    assert manifest["tracks"][0]["startOffsetMs"] == 100
    assert manifest["tracks"][0]["frames"] == 2880
    assert writer.file.closed


def test_completed_session_keeps_sources_separate(host):
    track(host, "local", kind="local")
    track(host, "remote", kind="remote")
    audio(host, "local", offset=10)
    audio(host, "remote", offset=250)
    result = host.handle({"type": "stop"})
    assert result["type"] == "stopped"
    manifest = load_session(Path(result["path"]) / "session.json")
    assert manifest["state"] == "completed"
    assert [t["startOffsetMs"] for t in manifest["tracks"]] == [10, 250]
    assert len({t["file"] for t in manifest["tracks"]}) == 2
    with pytest.raises(ValueError):
        audio(host, "local", sequence=1)


@pytest.mark.parametrize(
    "changes",
    [
        dict(sampleRate=0),
        dict(sampleRate=True),
        dict(channels=3),
        dict(kind="unknown"),
        dict(id=""),
        dict(id="a" * 201),
    ],
)
def test_reject_invalid_track(host, changes):
    with pytest.raises(ValueError):
        track(host, **changes)


@pytest.mark.parametrize("offset", [-1, float("nan"), float("inf"), 86400001, True])
def test_reject_invalid_timestamps(host, offset):
    track(host)
    with pytest.raises(ValueError):
        audio(host, offset=offset)


def test_reject_duplicate_and_missing_sequence(host):
    track(host)
    with pytest.raises(ValueError):
        track(host)
    with pytest.raises(ValueError):
        audio(host, sequence=1)
    audio(host)
    with pytest.raises(ValueError):
        audio(host)


def test_reject_stale_and_huge_gaps(host):
    track(host)
    audio(host, offset=200)
    for offset in (0, 100000):
        with pytest.raises(ValueError):
            audio(host, offset=offset, sequence=1)


def test_id_cannot_escape_recording_directory(host):
    track(host, "../escape")
    audio(host, "../escape")
    host.handle({"type": "stop"})
    assert not (host.directory / "escape").exists()
    manifest = host.session.manifest
    manifest["tracks"][0]["file"] = "../escape.wav"
    host.session.checkpoint()
    with pytest.raises(ValueError):
        load_session(host.session.directory / "session.json")


def test_empty_session_reports_problem(host):
    assert "No audio samples" in host.handle({"type": "stop"})["problem"]


def test_protocol_version_is_explicit(tmp_path):
    host = BrowserHost(tmp_path)
    with pytest.raises(ValueError, match="version"):
        host.handle({"type": "start", "version": 2})
    host.handle({"type": "start", "version": 1})
    host.close()
