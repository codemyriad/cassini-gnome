# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual streamed PCM/cache fingerprints; injected native infer only."""

import json
import sys
import types
import wave

import pytest

from cassini_viewer import worker_diarization as worker

pytest.importorskip("numpy")  # Core/Whistle-light profiles need no PCM worker dependencies.


def audio(path, frames=16000, sample=100):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", ""))
        stream.writeframes(sample.to_bytes(2, "little", signed=True) * frames)
    return dict(id="room", path=str(path), frames=frames, sampleRate=16000, speaker="others", kind="system", offsetMs=0)


@pytest.fixture
def native(monkeypatch):
    calls = []

    class Native:
        def __init__(self, *_args):
            calls.append("construct")

        def infer(self, samples, _notify):
            assert samples.ndim == 1 and len(samples) == 16000
            calls.append("infer")
            return dict(
                turns=[dict(speaker=0, startMs=0, endMs=400), dict(speaker=1, startMs=500, endMs=900)],
                inferenceSeconds=0.01,
            )

        def close(self):
            calls.append("close")

    monkeypatch.setitem(sys.modules, "cassini_viewer.nemotron", types.SimpleNamespace(Nemotron=Native))
    return calls


@pytest.mark.parametrize("case", ["short", "remote"])
def test_short_audio_and_participant_tracks_never_construct_model(tmp_path, native, case):
    track = audio(tmp_path / "track.wav", frames=3199 if case == "short" else 16000)
    if case == "remote":
        track.update(speaker="track-person", kind="remote")
    events = []
    result = worker.run([track], dict(scratch=tmp_path), lambda kind, **value: events.append(dict(type=kind, **value)))
    assert not native and not result["diagnostics"]
    assert result["groupIds"] == (["others"] if case == "short" else [])
    if case == "short":
        assert events[-1]["turns"] == []


@pytest.mark.parametrize("change", [None, "audio", "offset", "model", "params"])
def test_reuse_only_identical_group_input_and_model_params(tmp_path, native, change):
    track = audio(tmp_path / "track.wav")
    cache = tmp_path / "speaker-turns.json"
    options = dict(scratch=tmp_path, modelPath="model", runtimePath="runtime", storedTurnsPath=cache)
    events = []

    def notify(kind, **value):
        if kind == "diarized":
            events.append(value)

    result = worker.run([track], options, notify)
    assert native == ["construct", "infer", "close"]
    batch = events[-1]
    data = dict(
        format=worker.FORMAT,
        model=result["model"],
        params=result["params"],
        groups=[
            dict(
                id=batch["groupId"],
                durationMs=batch["durationMs"],
                inputSHA256=batch["inputSHA256"],
                turns=batch["turns"],
            )
        ],
    )
    if change == "audio":
        audio(tmp_path / "track.wav", sample=200)
    elif change == "offset":
        # Change both extent and PCM placement while keeping one second total.
        track.update(offsetMs=100)
        audio(tmp_path / "track.wav", frames=14400)
    elif change == "model":
        data["model"] = "unrelated-revision"
    elif change == "params":
        data["params"] = dict(minDurationOn=0.4, minDurationOff=0.5)
    cache.write_text(json.dumps(data))
    native.clear()
    events.clear()
    result = worker.run([track], options, notify)
    assert not result["diagnostics"]
    assert events[-1]["reused"] is (change is None)
    assert native == ([] if change is None else ["construct", "infer", "close"])
