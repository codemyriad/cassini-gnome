# SPDX-License-Identifier: GPL-3.0-or-later
"""D64: a group whose `<group>~n` voice would reuse another speaker id stays undiarized."""

import copy
import logging

import pytest
from test_voice_source import TURNS_A, TURNS_B, WORDS, X, Y, interrupted, run, scripted, speaker, stamp, words_of

from cassini_talk.diarization import voice_id_collisions
from cassini_talk.recording_session import RecordingSession
from cassini_talk.speaker_edits import save_name

THREE = [dict(text="Three", startMs=100, endMs=400)]
OTHER = [dict(text="Uno", startMs=510, endMs=850), dict(text="Due", startMs=1010, endMs=1800)]


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return root


def capture(root, sources, *, silent=()):
    """One 2 s tab track per source id; a source in `silent` is all zeros (D56)."""
    session = RecordingSession(root, source="gnome")
    for n, source in enumerate(sources):
        session.add_track(f"tab{n}", "A tab", "tab", 16000, 1, source_track_id=source)
        session.append(f"tab{n}", (b"\x00\x00" if source in silent else b"\x01\x00") * 32000, 0)
    session.finish()
    return session.directory


def check_speakers(metadata, expected, *, apart=()):
    """No duplicate id; the sources in `apart` keep distinct colours."""
    ids = [s["id"] for s in metadata["speakers"]]
    assert sorted(ids) == sorted(expected) and len(set(ids)) == len(ids)
    colours = [speaker(metadata, identifier)["colourIndex"] for identifier in apart]
    assert len(set(colours)) == len(colours)


@pytest.mark.parametrize("others", [["s~1"], ["s~2"], ["s~1", "s~2"]])
def test_source_named_like_a_voice_keeps_the_group_one_voice(root, caplog, others):
    directory = capture(root, ["s", *others])
    words = {"track-s": WORDS, **{f"track-{o}": [dict(w, text=f"{w['text']} {o}") for w in THREE] for o in others}}
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        metadata = run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    assert words_of(directory) == {"One": "track-s", "Two": "track-s"} | {f"Three {o}": f"track-{o}" for o in others}
    sources = ["track-s", *(f"track-{o}" for o in others)]
    check_speakers(metadata, sources, apart=sources)
    assert not any("voiceSource" in s for s in metadata["speakers"])
    collided = ",".join(f"track-{o}" for o in others)
    assert f"voiceIdCollision group=track-s ids={collided}" in caplog.text
    # Stored as D11 stores an inconclusive group: no format change.
    assert metadata["recordingDetails"]["writingDown"]["speakerSeparation"]["status"] == "completed"


@pytest.mark.parametrize(
    ("others", "turns", "voices"),
    [
        ([], TURNS_A, ("track-s~1", "track-s~2")),
        (["s~3"], TURNS_A, ("track-s~1", "track-s~2")),
        # Only the ids written count: no word goes to ordinal 1 here.
        (["s~1"], TURNS_B, ("track-s~2", "track-s~3")),
    ],
)
def test_without_a_collision_diarization_is_unchanged(root, caplog, others, turns, voices):
    directory = capture(root, ["s", *others])
    words = {"track-s": WORDS, **{f"track-{o}": THREE for o in others}}
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        metadata = run(root, directory, scripted({"track-s": (X, turns, False)}, words))
    expected = {"One": voices[0], "Two": voices[1]}
    if others:
        expected["Three"] = f"track-{others[0]}"
    assert words_of(directory) == expected
    check_speakers(metadata, ["track-s", *voices, *(f"track-{o}" for o in others)])
    for voice in voices:
        assert speaker(metadata, voice)["voiceSource"] == stamp(X, turns)
    assert "voiceIdCollision" not in caplog.text


def test_lane_group_collides_with_a_source_named_like_its_voice(root, caplog):
    """Two overlapping spans of `s` make R64 lane `track-s:overlap:1`; another source is literally its `~1`."""
    directory = capture(root, ["s", "s", "s:overlap:1~1"])
    lane, collider = "track-s:overlap:1", "track-s:overlap:1~1"
    words = {lane: WORDS, collider: THREE}
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        metadata = run(root, directory, scripted({lane: (X, TURNS_A, False)}, words))
    assert words_of(directory) == {"One": lane, "Two": lane, "Three": collider}
    check_speakers(metadata, ["track-s", lane, collider], apart=["track-s", lane, collider])
    assert f"voiceIdCollision group={lane} ids={collider}" in caplog.text


def test_silent_source_keeps_its_reserved_id(root, caplog):
    """A D56 silent track has no words, but its speaker row is still another speaker's id."""
    directory = capture(root, ["s", "s~1"], silent={"s~1"})
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        metadata = run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, {"track-s": WORDS}))
    assert words_of(directory) == {"One": "track-s", "Two": "track-s"}
    check_speakers(metadata, ["track-s", "track-s~1"], apart=["track-s", "track-s~1"])
    assert "voiceIdCollision group=track-s ids=track-s~1" in caplog.text


def test_colliding_source_still_diarizes_into_its_own_voices(root, caplog):
    directory = capture(root, ["s", "s~1"])
    words = {"track-s": WORDS, "track-s~1": OTHER}
    groups = {"track-s": (X, TURNS_A, False), "track-s~1": (Y, TURNS_A, False)}
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        metadata = run(root, directory, scripted(groups, words))
    assert words_of(directory) == {"One": "track-s", "Two": "track-s", "Uno": "track-s~1~1", "Due": "track-s~1~2"}
    check_speakers(metadata, ["track-s", "track-s~1", "track-s~1~1", "track-s~1~2"], apart=["track-s", "track-s~1"])
    assert speaker(metadata, "track-s~1~2")["voiceSource"] == stamp(Y, TURNS_A)
    assert "voiceIdCollision group=track-s ids=track-s~1" in caplog.text
    assert "group=track-s~1 " not in caplog.text


def test_another_groups_voice_is_a_backstop_collision():
    # Two groups cannot form one `<group>~n` id; the helper still refuses both.
    planned = {"a": ["a~1", "a~2"], "b": ["b~1", "b~2"], "c": ["x~1", "c~2"], "d": ["x~1", "d~2"]}
    assert voice_id_collisions(planned, {"a~2"}) == {"a": ["a~2"], "c": ["x~1"], "d": ["x~1"]}


def test_rediarization_that_starts_colliding_keeps_the_source_and_resets_old_voices(root, caplog):
    directory = capture(root, ["s", "s~3"])
    words = {"track-s": WORDS, "track-s~3": THREE}
    first = run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    assert words_of(directory) == {"One": "track-s~1", "Two": "track-s~2", "Three": "track-s~3"}
    check_speakers(first, ["track-s", "track-s~1", "track-s~2", "track-s~3"])
    save_name(directory, "track-s~2", "Mira")
    save_name(directory, "track-s~3", "Sam")
    interrupted(directory)

    # TURNS_B would write track-s~2 and track-s~3, and track-s~3 is a source.
    with caplog.at_level(logging.WARNING, "cassini_talk.diarization"):
        second = run(root, directory, scripted({"track-s": (X, TURNS_B, False)}, words), resume=True)
    assert words_of(directory) == {"One": "track-s", "Two": "track-s", "Three": "track-s~3"}
    check_speakers(second, ["track-s", "track-s~1", "track-s~2", "track-s~3"])
    # G12: the re-diarized group rebinds its old voices, as for an inconclusive run.
    assert speaker(second, "track-s~2")["name"] is None
    assert speaker(second, "track-s~2")["voiceSource"] == stamp(X, TURNS_B)
    sam = speaker(second, "track-s~3")
    assert sam["name"] == "Sam" and sam["provenance"] == "user" and "voiceSource" not in sam
    assert "voiceIdCollision group=track-s ids=track-s~3" in caplog.text


@pytest.mark.parametrize("background", [False, True])
def test_voice_step_reads_each_source_only_from_its_own_audio(root, tmp_path, monkeypatch, background):
    """No print for `track-s~1` from group s's audio: only written voices are embedded."""
    pytest.importorskip("numpy")
    from cassini_talk import campplus, nemotron, worker_voices
    from cassini_talk.library import LibraryScanner
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.transcription import prepare_tracks

    directory = capture(root, ["s", "s~1"])
    words = {"track-s": WORDS, "track-s~1": THREE}
    run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    written = {s["id"] for s in read_metadata(directory)["speakers"]}
    (meeting,) = LibraryScanner(root).scan().meetings
    tracks, _speakers = prepare_tracks(meeting)
    calls, embedded = [], []

    class Diarizer:
        def __init__(self, *_args):
            pass

        def infer(self, _samples, _progress):
            return dict(turns=copy.deepcopy(TURNS_A))

        def close(self):
            pass

    class Extractor(Diarizer):
        def embed(self, chunks):
            return None

    real = worker_voices.select_windows

    def spy(turns, selected, *args, **kwargs):
        result = real(turns, selected, *args, **kwargs)
        calls.append(({w["speaker"] for w in selected}, set(kwargs["own_labels"])))
        embedded.extend(result)
        return result

    monkeypatch.setattr(nemotron, "Nemotron", Diarizer)
    monkeypatch.setattr(campplus, "SpeakerExtractor", Extractor)
    monkeypatch.setattr(worker_voices, "select_windows", spy)
    monkeypatch.setattr(worker_voices.os, "nice", lambda _n: 0)
    events = []
    options = dict(
        directory=str(directory),
        prepareMissing=background,
        diarization=dict(modelPath="d", runtimePath="r"),
        modelPath="m",
        runtimePath="r",
        origin="own",
        scratch=str(tmp_path / "scratch"),
    )
    worker_voices.run(tracks, options, lambda kind, **_values: events.append(kind))
    assert events[-1] == "voices-complete"
    assert calls == [({"track-s"}, set()), ({"track-s~1"}, set())]
    assert set(embedded) <= written
