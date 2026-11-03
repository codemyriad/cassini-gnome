# SPDX-License-Identifier: GPL-3.0-or-later
"""D64: a group whose `<group>~n` voice would reuse another speaker id stays undiarized."""

import copy
import hashlib
import json
import logging
from uuid import uuid4

import pytest
from test_voice_source import TURNS_A, TURNS_B, WORDS, X, Y, interrupted, run, scripted, speaker, stamp, words_of

from cassini_talk.diarization import voice_id_collisions
from cassini_talk.meeting_store import read_metadata, update_metadata
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


def break_audio(directory, sources, how):
    """Every track of `sources` loses its file (`missing`) or holds bytes that are not a WAV (`unreadable`)."""
    for track in json.loads((directory / "session.json").read_text())["tracks"]:
        if track.get("sourceTrackId") in sources:
            path = directory / track["file"]
            if how == "missing":
                path.unlink()
            else:
                path.write_bytes(b"not a wav")


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


def test_old_voice_row_of_a_source_id_is_reset(root, caplog):
    """meeting.json still holds track-s~1 as group s's named voice; now a source owns that id."""
    directory = capture(root, ["s", "s~1"])
    rows = [s for s in read_metadata(directory)["speakers"] if s["id"] != "track-s~1"]
    rows.append(dict(id="track-s~1", name="Mira", provenance="user", colourIndex=1, voiceSource=stamp(X, TURNS_A)))
    update_metadata(directory, speakers=rows, speakerMerges=[{"from": "track-s~1", "into": "track-s"}])
    words = {"track-s": WORDS, "track-s~1": THREE}
    with caplog.at_level(logging.WARNING):
        metadata = run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    assert words_of(directory) == {"One": "track-s", "Two": "track-s", "Three": "track-s~1"}
    source = speaker(metadata, "track-s~1")
    assert source["name"] is None and source["provenance"] == "unnamed" and "voiceSource" not in source
    # The old voice's merge would show the source's words as group s.
    assert metadata.get("speakerMerges", []) == []
    assert "voiceNameReset id=track-s~1 old=user named=True" in caplog.text


def voice_step(root, tmp_path, monkeypatch, directory, background):
    """Run the voice step on fake models; return the words each print read and what was embedded."""
    pytest.importorskip("numpy")
    from cassini_talk import campplus, nemotron, worker_voices
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks, reserved_speakers

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
        # As the app sends it (TranscriptionQueue._run and _voice_job_body).
        reservedSpeakers=sorted(reserved_speakers(meeting, tracks)),
    )
    worker_voices.run(tracks, options, lambda kind, **_values: events.append(kind))
    assert events[-1] == "voices-complete"
    return calls, embedded


@pytest.mark.parametrize("background", [False, True])
def test_voice_step_reads_each_source_only_from_its_own_audio(root, tmp_path, monkeypatch, background):
    """No print for `track-s~1` from group s's audio: only written voices are embedded."""
    directory = capture(root, ["s", "s~1"])
    run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, {"track-s": WORDS, "track-s~1": THREE}))
    written = {s["id"] for s in read_metadata(directory)["speakers"]}
    calls, embedded = voice_step(root, tmp_path, monkeypatch, directory, background)
    assert calls == [({"track-s"}, set()), ({"track-s~1"}, set())]
    assert set(embedded) <= written


@pytest.mark.parametrize("background", [False, True])
def test_voice_step_reads_the_voices_of_a_colliding_source(root, tmp_path, monkeypatch, background):
    """`track-s~1~2` is a voice of group s~1, never of group s, so preparation never drops it."""
    directory = capture(root, ["s", "s~1"])
    groups = {"track-s": (X, TURNS_A, False), "track-s~1": (Y, TURNS_A, False)}
    run(root, directory, scripted(groups, {"track-s": WORDS, "track-s~1": OTHER}))
    voices = {"track-s~1~1", "track-s~1~2"}
    calls, embedded = voice_step(root, tmp_path, monkeypatch, directory, background)
    assert [selected for selected, _labels in calls] == [{"track-s"}, voices]
    assert set(embedded) <= {s["id"] for s in read_metadata(directory)["speakers"]}


def options_spy(client, seen):
    """The scripted client, keeping the options of each voice request."""

    class Client(client):
        def voiceprints(self, tracks, options, heartbeat):
            seen.append(options)
            return super().voiceprints(tracks, options, heartbeat)

    return Client


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_source_whose_audio_is_gone_keeps_its_id_and_its_name(root, tmp_path, caplog, how):
    """Codex #185: s~1's file goes; re-diarizing s must not write track-s~1 as its voice over Sam's row."""
    from cassini_talk.voice_store import VoiceStore

    directory = capture(root, ["s", "s~1"])
    words = {"track-s": WORDS, "track-s~1": THREE}
    run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    save_name(directory, "track-s~1", "Sam")
    sam = speaker(read_metadata(directory), "track-s~1")
    interrupted(directory)
    break_audio(directory, {"s~1"}, how)

    store, seen = VoiceStore(tmp_path / "private"), []
    store.refresh()
    with caplog.at_level(logging.WARNING):
        client = options_spy(scripted({"track-s": (X, TURNS_A, False)}, words), seen)
        metadata = run(root, directory, client, resume=True, store=store)
    assert {w: s for w, s in words_of(directory).items() if s != "track-s~1"} == {"One": "track-s", "Two": "track-s"}
    check_speakers(metadata, ["track-s", "track-s~1"])
    assert speaker(metadata, "track-s~1") == sam and sam["name"] == "Sam" and "voiceSource" not in sam
    assert "voiceIdCollision group=track-s ids=track-s~1" in caplog.text
    assert "voiceNameReset" not in caplog.text
    # The voice step gets the same reservations.
    assert "track-s~1" in seen[-1]["reservedSpeakers"]


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_missing_lane_track_keeps_its_lane_id_in_the_write_down_and_the_voice_step(
    root, tmp_path, monkeypatch, caplog, how
):
    """s~1's two overlapping tracks hold track-s~1 and lane track-s~1:overlap:1; both files go."""
    directory = capture(root, ["s", "s~1", "s~1"])
    lane = "track-s~1:overlap:1"
    words = {"track-s": WORDS, "track-s~1": THREE, lane: OTHER}
    run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words))
    assert set(words_of(directory).values()) == {"track-s", "track-s~1", lane}
    save_name(directory, "track-s~1", "Sam")
    save_name(directory, lane, "Lia")
    before = read_metadata(directory)
    break_audio(directory, {"s~1"}, how)

    # The background voice step reads the saved words: only group s's own come from its audio.
    calls, embedded = voice_step(root, tmp_path, monkeypatch, directory, True)
    assert calls == [({"track-s"}, set())]
    assert set(embedded) <= {"track-s"}

    interrupted(directory)
    with caplog.at_level(logging.WARNING):
        metadata = run(root, directory, scripted({"track-s": (X, TURNS_A, False)}, words), resume=True)
    assert {w: s for w, s in words_of(directory).items() if s == "track-s"} == {"One": "track-s", "Two": "track-s"}
    check_speakers(metadata, ["track-s", "track-s~1", lane])
    for identifier in ("track-s~1", lane):
        assert speaker(metadata, identifier) == speaker(before, identifier)
    assert "voiceIdCollision group=track-s ids=track-s~1" in caplog.text
    assert "voiceNameReset" not in caplog.text


def received_meeting(root, tracks):
    """A Received folder whose original's x["codemyriad.io"] lists `tracks` and no session.json."""
    from test_library import write_metadata
    from test_turn_breaks import foreign_file

    folder = root / f"Received-{uuid4()}"
    folder.mkdir(parents=True)
    original = foreign_file(folder / "original.opus", tracks)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    write_metadata(folder, received=dict(originalName=original.name, sha256=digest, importedAtMs=1793107080000))
    return folder


def test_a_received_files_listed_track_keeps_its_id(root, caplog):
    """The file's x tracks list `original~1`: its id is track-original~1, the one group's would-be voice."""
    from test_turn_breaks import stored_track

    directory = received_meeting(root, [dict(stored_track("a"), sourceTrackId="original~1")])
    rows = read_metadata(directory)["speakers"]
    sam = dict(id="track-original~1", name="Sam", provenance="user", colourIndex=1)
    update_metadata(directory, speakers=[*rows, sam])
    with caplog.at_level(logging.WARNING):
        metadata = run(root, directory, scripted({"track-original": (X, TURNS_A, False)}, {"track-original": WORDS}))
    assert words_of(directory) == {"One": "track-original", "Two": "track-original"}
    assert speaker(metadata, "track-original~1") == sam
    ids = [s["id"] for s in metadata["speakers"]]
    assert len(set(ids)) == len(ids) and not any("voiceSource" in s for s in metadata["speakers"])
    assert "voiceIdCollision group=track-original ids=track-original~1" in caplog.text
    assert "voiceNameReset" not in caplog.text
