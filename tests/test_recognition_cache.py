# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1120: the v2 segment cache schema and the non-UI TranscriptionMode seam.

No NumPy or gi here, so the minimal-environment job runs these too; the
pipeline equivalence checks live in test_recognition_cache_pipeline.py.
"""

import hashlib
import json
from pathlib import Path

import pytest
from durable import Finished
from test_library import capture

from cassini_talk import recognition_cache as cache
from cassini_talk.library import LibraryScanner
from cassini_talk.transcription import (
    TranscriptionMode,
    TranscriptionQueue,
    auto_write_enabled,
    recognition_cache_model,
    should_auto_write,
)

KEY = "ab" * 32 + ":16000"
MODEL = "whistle@" + "0" * 64


def entry(*words):
    return dict(
        text=" ".join(w[2] for w in words), language="en", words=[dict(start=a, end=b, text=t) for a, b, t in words]
    )


@pytest.fixture(autouse=True)
def configured_library(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))


def test_key_is_the_float32_le_digest_and_sample_count():
    raw = b"\x00\x00\x80\x3f" * 3  # three float32 1.0 values
    assert cache.samples_key(raw) == hashlib.sha256(raw).hexdigest() + ":3"
    assert cache.segment_key("f" * 64, 7) == "f" * 64 + ":7"
    with pytest.raises(ValueError):
        cache.samples_key(b"\x00" * 6)


def test_entry_keeps_raw_segment_relative_output_only():
    result = dict(
        text=" hi there",
        language=None,
        words=[dict(word="hi", start=0, end=0.25), dict(text="there", start=0.3, end=0.5)],
    )
    value = cache.entry(result)
    assert value == dict(
        text=" hi there",
        language="und",
        words=[dict(start=0.0, end=0.25, text="hi"), dict(start=0.3, end=0.5, text="there")],
    )
    assert all(type(w["start"]) is float and type(w["end"]) is float for w in value["words"])
    assert cache.valid_entry(KEY, value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["words"][0].update(start=-0.01),
        lambda e: e["words"][0].update(end=0.05),  # ends before it starts
        lambda e: e["words"][0].update(start=float("nan")),
        lambda e: e["words"][0].update(end=float("inf")),
        lambda e: e["words"][0].update(start=True),
        lambda e: e["words"][0].update(start="0.1"),
        lambda e: e["words"][0].update(end=1 + 30 + 0.001),  # past the PCM plus slack
        lambda e: e["words"][0].pop("text"),
        lambda e: e["words"][0].update(startMs=100),  # v1 placed words are not raw output
        lambda e: e["words"][0].update(text=None),
        lambda e: e.update(language=""),
        lambda e: e.update(text=None),
        lambda e: e.update(words={}),
        lambda e: e.update(offsetMs=0),
        lambda e: e.pop("language"),
    ],
)
def test_invalid_entries_are_rejected(mutate):
    value = entry((0.1, 0.4, "one"), (0.5, 0.9, "two"))
    assert cache.valid_entry(KEY, value)
    mutate(value)
    assert not cache.valid_entry(KEY, value)


@pytest.mark.parametrize(
    "key", ["ab" * 32, "AB" * 32 + ":1", "ab" * 32 + ":0", "ab" * 32 + ":-1", "ab" * 31 + ":16", 7]
)
def test_invalid_keys_are_rejected(key):
    assert not cache.valid_entry(key, entry((0.1, 0.4, "one")))


def test_parse_reads_only_this_model_and_format_and_drops_bad_segments():
    good = entry((0.1, 0.4, "one"))
    bad = entry((0.4, 0.1, "backwards"))
    raw = json.dumps(cache.document(MODEL, {KEY: good, "cd" * 32 + ":8": bad})).encode()
    assert cache.parse(raw, MODEL) == {KEY: good}
    assert cache.parse(raw, "whistle@" + "1" * 64) == {}, "a different bundle never reuses words"
    assert cache.parse(raw, "parakeet-ultra-0.6b-int8-v2@x") == {}
    assert cache.parse(raw[: len(raw) // 2], MODEL) == {}, "truncated"
    assert cache.parse(b"\xff" + raw, MODEL) == {}, "corrupted"
    assert cache.parse(raw.replace(b"0.1", b"NaN"), MODEL) == {}, "non-finite JSON"
    assert cache.parse(b"[]", MODEL) == {}
    # D51: v1 (placed words keyed on placement) is an unrecognised version.
    v1 = dict(format="cassini.recognition-cache.v1", model=MODEL, segments={"a" * 64: good})
    assert cache.parse(json.dumps(v1).encode(), MODEL) == {}
    oversized = b" " * (cache.MAX_BYTES + 1)
    assert cache.parse(oversized, MODEL) == {}


def test_read_never_follows_links_and_tolerates_missing_files(tmp_path):
    path = tmp_path / cache.FILE
    assert cache.read(path, MODEL) == {}
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(cache.document(MODEL, {KEY: entry((0.1, 0.2, "x"))})))
    path.symlink_to(target)
    assert cache.read(path, MODEL) == {}
    path.unlink()
    path.write_bytes(target.read_bytes())
    assert set(cache.read(path, MODEL)) == {KEY}
    assert cache.read(path, None) == {} and cache.read(None, MODEL) == {}


def test_model_identity_names_the_checkpoint_or_bundle(tmp_path):
    import cassini_talk
    from cassini_talk.parakeet_model import MODEL_ID, MODEL_TAG, REVISION

    parakeet = recognition_cache_model(MODEL_ID, tmp_path)
    runtime = (Path(cassini_talk.__file__).with_name("sherpa-pins.json")).read_bytes()
    assert parakeet == f"{MODEL_TAG}+sherpa@{hashlib.sha256(runtime).hexdigest()}" and REVISION in parakeet
    assert recognition_cache_model("whistle", tmp_path) is None, "an unidentified bundle caches nothing"
    (tmp_path / "pins.json").write_text('{"installed": {"whistle.cact": "1"}}')
    first = recognition_cache_model("whistle", tmp_path)
    assert first.startswith("whistle@") and first != MODEL_TAG
    (tmp_path / "pins.json").write_text('{"installed": {"whistle.cact": "2"}}')
    assert recognition_cache_model("whistle", tmp_path) != first


def test_parakeet_identity_changes_with_the_sherpa_runtime_pins(tmp_path):
    pins = tmp_path / "sherpa-pins.json"
    assert cache.parakeet_identity("parakeet@1", pins) is None, "an unpinned runtime caches nothing"
    pins.write_text('{"sherpa-onnx-go-linux": {"files": {"libonnxruntime.so": "1"}}}')
    first = cache.parakeet_identity("parakeet@1", pins)
    assert first.startswith("parakeet@1+sherpa@")
    pins.write_text('{"sherpa-onnx-go-linux": {"files": {"libonnxruntime.so": "2"}}}')
    assert cache.parakeet_identity("parakeet@1", pins) != first, "a runtime upgrade never reuses old output"
    assert cache.parakeet_identity("parakeet@2", pins) != cache.parakeet_identity("parakeet@1", pins)


def test_auto_write_follows_transcription_enabled_and_mode():
    from types import SimpleNamespace

    assert auto_write_enabled(SimpleNamespace(transcription_enabled=True)), "afterStop is the default"
    assert not auto_write_enabled(SimpleNamespace(transcription_enabled=False))
    assert not auto_write_enabled(SimpleNamespace())
    for mode, expected in (("afterStop", True), ("transcribeWhileRecording", True), ("recordOnly", False)):
        assert auto_write_enabled(SimpleNamespace(transcription_enabled=True, transcription_mode=mode)) is expected
        assert not auto_write_enabled(SimpleNamespace(transcription_enabled=False, transcription_mode=mode))
    with pytest.raises(ValueError):
        auto_write_enabled(SimpleNamespace(transcription_enabled=True, transcription_mode="live"))


def test_transcription_mode_values_and_validation():
    assert [m.value for m in TranscriptionMode] == ["recordOnly", "afterStop", "transcribeWhileRecording"]
    assert TranscriptionMode.parse("record_only") is TranscriptionMode.RECORD_ONLY
    assert TranscriptionMode.parse("afterStop") is TranscriptionMode.AFTER_STOP
    assert TranscriptionMode.parse("transcribe_while_recording") is TranscriptionMode.TRANSCRIBE_WHILE_RECORDING
    for value in ("", "AFTER_STOP", "live", None, 1):
        with pytest.raises(ValueError):
            TranscriptionMode.parse(value)
    with pytest.raises(ValueError):
        should_auto_write(None, {}, "sometimes")


def test_application_default_is_after_stop():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/cassini_talk/main.py").read_text()
    default = next(
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "transcription_mode" for t in node.targets)
    )
    assert TranscriptionMode.parse(default) is TranscriptionMode.AFTER_STOP


def test_record_only_leaves_a_stopped_recording_audio_only_until_transcribe(tmp_path):
    session = capture(tmp_path, completed=False)
    before = LibraryScanner(tmp_path).scan().meetings[0]
    session.finish()
    after = LibraryScanner(tmp_path).scan().meetings[0]
    previous = {before.key: (before.lifecycle, before.capture_state)}
    assert should_auto_write(after, previous)
    assert should_auto_write(after, previous, TranscriptionMode.AFTER_STOP)
    assert should_auto_write(after, previous, "transcribeWhileRecording"), "finalizes as afterStop until D-1121"
    assert not should_auto_write(after, previous, TranscriptionMode.RECORD_ONLY)
    assert not should_auto_write(after, previous, "record_only")
    assert after.transcript == "none" and after.can_write_down

    started = []
    completed = Finished()

    class Client:
        def __init__(self, *_args, **_kw):
            started.append(True)

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(dict(type="planned", etaMinutes=0, segments=1))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=100, text="Later")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, completed, client_factory=Client, bundle=tmp_path)
    try:
        assert not started, "no worker exists before the user asks"
        # The user's Transcribe is the explicit enqueue, in any mode.
        assert queue.enqueue(after.directory)
        completed.wait(1)
        done = LibraryScanner(tmp_path).scan().meetings[0]
        assert done.transcript == "done" and [w.text for w in done.words] == ["Later"]
    finally:
        queue.close()
