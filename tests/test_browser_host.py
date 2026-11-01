# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise production stdio and cleanup, without launching any desktop app."""

import base64
import io
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_talk.browser_host import main
from cassini_talk.recording_session import ProtocolError, RecordingSession, WAVWriter, read_message, write_message

ROOT = Path(__file__).resolve().parents[1]
START = {"type": "start"}
TRACK = dict(type="track", id="one", label=None, kind="remote", sampleRate=48000, channels=1)
AUDIO = dict(type="audio", id="one", sequence=0, offsetMs=0, pcm=base64.b64encode(b"\x01\x00" * 480).decode())


def invoke(tmp_path, messages, **options):
    incoming, outgoing = io.BytesIO(), io.BytesIO()
    for message in messages:
        write_message(incoming, message)
    incoming.seek(0)
    code = main(tmp_path, input_stream=incoming, output_stream=outgoing, **options)
    outgoing.seek(0)
    replies = []
    while (reply := read_message(outgoing)) is not None:
        replies.append(reply)
    return code, replies


def test_canonical_conformance(tmp_path):
    # The runner validates against the JSON schemas; meson treats these as optional too.
    pytest.importorskip("jsonschema")
    pytest.importorskip("rfc3339_validator")
    result = subprocess.run(
        [sys.executable, str(ROOT / "browser-protocol/conformance/run.py"), str(ROOT / "tests/conformance_host.py")],
        # The runner makes each case's private Library under TMPDIR.
        env={**os.environ, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    count = len(json.loads((ROOT / "browser-protocol/conformance/vectors.json").read_text()))
    assert f"{count} passed, 0 failed" in result.stdout


def test_pinned_contract_integrity():
    subprocess.run(
        [sys.executable, str(ROOT / "browser-protocol/verify.py"), "--pinned", str(ROOT / "browser-extension")],
        check=True,
    )


def test_app_failure_preserves_completed_session(tmp_path, caplog):
    calls = []

    def launch(args, **kwargs):
        calls.append(args)
        raise FileNotFoundError("test app missing")

    code, replies = invoke(
        tmp_path, [START, TRACK, AUDIO, {"type": "stop"}], app_path="/isolated/bin/cassini-talk", launcher=launch
    )
    assert code == 0 and replies[-1]["type"] == "stopped"
    manifest = json.loads((Path(replies[-1]["path"]) / "session.json").read_text())
    assert manifest["state"] == "completed"
    assert calls == [["/isolated/bin/cassini-talk", str(Path(replies[-1]["path"]) / "session.json")]]
    assert "app could not start" in caplog.text
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["createdAt"])


def test_eof_finalizes_without_launching(tmp_path):
    code, replies = invoke(tmp_path, [START, TRACK, AUDIO], launcher=lambda *a, **k: pytest.fail("unexpected launch"))
    assert code == 0
    manifest = json.loads((Path(replies[0]["path"]) / "session.json").read_text())
    assert manifest["state"] == "interrupted"
    assert manifest["tracks"][0]["frames"] == 480


@pytest.mark.parametrize(
    "payload",
    [
        b'{"type":"start","type":"stop"}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":"\xff"}',
        b'{"a":"\\ud800"}',
        b'{"a":' + b"[" * 64 + b"0" + b"]" * 64 + b"}",
    ],
)
def test_strict_framing_error_response(tmp_path, payload):
    incoming = io.BytesIO(struct.pack("=I", len(payload)) + payload)
    outgoing = io.BytesIO()
    assert main(tmp_path, False, input_stream=incoming, output_stream=outgoing) == 1
    outgoing.seek(0)
    assert read_message(outgoing)["code"] == "framing"
    assert read_message(outgoing) is None
    assert not list(tmp_path.iterdir())


def test_close_failure_is_io_and_partial_session_survives(tmp_path, monkeypatch):
    original = WAVWriter.close

    def fail_close(self):
        original(self)
        raise OSError("disk sync failed")

    monkeypatch.setattr(WAVWriter, "close", fail_close)
    code, replies = invoke(tmp_path, [START, TRACK, AUDIO, {"type": "stop"}], open_completed=False)
    assert code == 1 and replies[-1]["code"] == "io"
    manifest = json.loads((Path(replies[0]["path"]) / "session.json").read_text())
    assert manifest["state"] == "interrupted" and manifest["problemKind"] == "io"
    assert manifest["tracks"][0]["frames"] == 480


def test_injected_wav_size_limit(tmp_path):
    writer = WAVWriter(tmp_path / "tiny.wav", 48000, 1, max_bytes=4)
    try:
        writer.append(b"\x01\x00" * 2)
        with pytest.raises(ProtocolError, match="size limit") as error:
            writer.append(b"\x01\x00")
        assert error.value.code == "limit" and writer.frames == 2
    finally:
        writer.close()


def test_checkpoint_uses_injected_clock_and_audio_before_metadata(tmp_path, monkeypatch):
    now = [0.0]
    session = RecordingSession(tmp_path, clock=lambda: now[0])
    session.add_track("one", None, "remote", 48000, 1)
    assert "startOffsetMs" not in session.manifest["tracks"][0]
    events = []
    sync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append(os.readlink(f"/proc/self/fd/{fd}")), sync(fd))[1])
    now[0] = 1.1
    session.append("one", b"\x01\x00" * 480, 50)
    assert events[0].endswith(".wav") and events[1].endswith("session.tmp")
    assert events[2] == str(session.directory)
    session.finish()


def test_append_failure_retains_io_reason(tmp_path, monkeypatch):
    def fail_append(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(WAVWriter, "append", fail_append)
    code, replies = invoke(tmp_path, [START, TRACK, AUDIO], open_completed=False)
    assert code == 1 and replies[-1]["code"] == "io"
    manifest = json.loads((Path(replies[0]["path"]) / "session.json").read_text())
    assert manifest["state"] == "interrupted" and manifest["problemKind"] == "io"


def test_huge_json_integer_offset_has_chunk_error(tmp_path):
    code, replies = invoke(tmp_path, [START, TRACK, {**AUDIO, "offsetMs": 10**400}], open_completed=False)
    assert code == 1 and replies[-1]["code"] == "chunk"


def test_installed_host_supplies_player_environment(tmp_path):
    share = tmp_path / "local/share"
    data = share / "cassini-talk"
    package = data / "cassini_talk"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "browser_host.py").write_text(
        "import json,os\ndef main(**kwargs):\n"
        " print(json.dumps(dict(kwargs,schemas=os.environ.get('GSETTINGS_SCHEMA_DIR'),data=os.environ.get('XDG_DATA_DIRS'))))\n"
        " return 0\n"
    )
    schemas = share / "glib-2.0/schemas"
    schemas.mkdir(parents=True)
    (schemas / "gschemas.compiled").touch()
    bindir = tmp_path / "local/bin"
    script = tmp_path / "host.py"
    script.write_text(
        (ROOT / "src/cassini-browser-host.in")
        .read_text()
        .replace("@pkgdatadir@", str(data))
        .replace("@bindir@", str(bindir))
    )
    env = {key: value for key, value in os.environ.items() if key not in ("GSETTINGS_SCHEMA_DIR", "XDG_DATA_DIRS")}
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert values["schemas"] == str(schemas)
    assert values["data"].split(":")[0] == str(share)
    assert values["app_path"] == str(bindir / "cassini-talk")
