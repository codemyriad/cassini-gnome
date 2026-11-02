# SPDX-License-Identifier: GPL-3.0-or-later
"""G12: a `<group>~n` name stays only on the diarization input it was made on."""

import copy
import hashlib
import json
import struct
import threading
import wave

import pytest
from test_voice_store import embedding

from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import read_metadata, update_metadata
from cassini_talk.nemotron_model import MODEL_TAG
from cassini_talk.recording_session import RecordingSession
from cassini_talk.speaker_edits import merge_into, save_name
from cassini_talk.transcription import REFERENCE, TranscriptionQueue
from cassini_talk.voice_store import VoiceStore
from cassini_talk.worker_diarization import FORMAT, PARAMS

X, Y = "a" * 64, "b" * 64
# Run 1: two voices. "One" is on others~1, "Two" on others~2.
TURNS_A = [dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)]
# Run 2 (more audio or another model): "One" moves to others~2, "Two" to others~3.
TURNS_B = [
    dict(startMs=0, endMs=400, speaker=0),
    dict(startMs=500, endMs=900, speaker=1),
    dict(startMs=1000, endMs=2000, speaker=2),
]
# Run 2 with one voice only: inconclusive, so the words fall back to D11 `others`.
TURNS_C = [dict(startMs=0, endMs=2000, speaker=0)]
WORDS = [dict(text="One", startMs=510, endMs=850), dict(text="Two", startMs=1010, endMs=1800)]


def stamp(sha, turns):
    """Independent restatement of the provisional R74 stamp."""
    order, rows = {}, []
    for turn in sorted(turns, key=lambda t: (t["startMs"], t["endMs"], t["speaker"])):
        order.setdefault(turn["speaker"], len(order) + 1)
        rows.append([turn["startMs"], turn["endMs"], order[turn["speaker"]]])
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    return dict(inputSHA256=sha, turnsSHA256=digest)


class Models:
    def request(self, directory, *, origin):
        return dict(modelPath="model", runtimePath="runtime", origin=origin)

    def measured(self, _factor):
        pass


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return root


def capture(root, *, tab=False):
    session = RecordingSession(root, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    if tab:
        session.add_track("tab7", "A tab", "tab", 16000, 1)
        session.append("tab7", b"\x01\x00" * 32000, 0)
    session.finish()
    return session.directory


def scripted(groups, words=None, prints=None):
    """groups: {groupId: (inputSHA256, turns, reused)}; words: {groupId: words}."""
    words = {"others": WORDS} if words is None else words

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def cancel(self):
            pass

        def process_tracks(self, tracks, notify):
            for order, track in enumerate(tracks):
                notify(
                    dict(
                        type="progress",
                        trackId=track["id"],
                        trackOrder=order,
                        language="en",
                        etaMinutes=1,
                        words=copy.deepcopy(words.get(track["speaker"], [])),
                    )
                )
            notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=1))
            for group, (sha, turns, reused) in groups.items():
                notify(
                    dict(
                        type="diarized",
                        groupId=group,
                        durationMs=2000,
                        inputSHA256=sha,
                        turns=copy.deepcopy(turns),
                        first=True,
                        final=True,
                        reused=reused,
                        etaMinutes=0,
                    )
                )
            return dict(
                diagnostics=[],
                diarization=dict(model=MODEL_TAG, params=PARAMS, groupIds=list(groups), diagnostics=[]),
            )

        def voiceprints(self, _tracks, _options, _heartbeat):
            return copy.deepcopy(prints or {})

    return Client


def run(root, directory, client, *, resume=False, store=None):
    """Wait on the queue's own finished callback; no fixed sleeps (E1)."""
    done = threading.Event()
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _directory: done.set(),
        bundle=root,
        root=root,
        client_factory=client,
        diarization_models=Models(),
        voice_models=Models() if store is not None else None,
        voice_store=store,
    )
    try:
        assert queue.enqueue(directory, resume=resume)
        assert done.wait(10), "Queue did not finish"
    finally:
        queue.close()
    metadata = read_metadata(directory)
    assert metadata["transcript"]["status"] == "done"
    return metadata


def interrupted(directory):
    """A crash between writing `~n` words and `done` leaves a resumable job."""
    update_metadata(directory, transcript=dict(read_metadata(directory)["transcript"], status="running"))


def speaker(metadata, identifier):
    return next(s for s in metadata["speakers"] if s["id"] == identifier)


def words_of(directory):
    body = json.loads((directory / "transcript.json").read_bytes())
    return {w["text"]: w["speaker"] for w in body["items"]}


def visible(root):
    (meeting,) = LibraryScanner(root).scan().meetings
    return meeting


def test_stamp_is_canonical_ordinal_json_of_usable_turns():
    from cassini_talk.diarization import turns_sha256, voice_source

    # Native labels are irrelevant, as are unusable turns and input order.
    relabelled = [dict(startMs=1000, endMs=2000, speaker=0), dict(startMs=0, endMs=900, speaker=5)]
    noisy = relabelled + [dict(startMs=50, endMs=60, speaker=-1), dict(startMs=70, endMs=70, speaker=1)]
    digest = hashlib.sha256(b"[[0,900,1],[1000,2000,2]]").hexdigest()
    assert turns_sha256(TURNS_A) == turns_sha256(relabelled) == turns_sha256(noisy) == digest
    assert voice_source(X, TURNS_A) == dict(inputSHA256=X, turnsSHA256=digest) == stamp(X, TURNS_A)
    assert turns_sha256(TURNS_B) != digest


@pytest.mark.parametrize(
    ("case", "sha", "turns", "reused", "kept"),
    [
        ("mira-moves", Y, TURNS_B, False, False),  # Brief §6.1
        ("reuse", X, TURNS_A, True, True),  # §6.2
        ("same-hash-new-turns", X, TURNS_B, False, False),  # §6.3
        ("new-hash-same-turns", Y, TURNS_A, False, False),  # §6.4
        ("inconclusive", Y, TURNS_C, False, False),  # Rule 3: the group was re-diarized.
    ],
)
def test_resumed_rerun_keeps_a_name_only_on_an_equal_stamp(root, case, sha, turns, reused, kept):
    directory = capture(root)
    first = run(root, directory, scripted({"others": (X, TURNS_A, False)}))
    assert words_of(directory) == {"One": "others~1", "Two": "others~2"}
    assert speaker(first, "others~2")["voiceSource"] == stamp(X, TURNS_A)
    save_name(directory, "others~2", "Mira")
    interrupted(directory)

    second = run(root, directory, scripted({"others": (sha, turns, reused)}), resume=True)
    mira = speaker(second, "others~2")
    assert mira["voiceSource"] == stamp(sha, turns)
    if kept:
        assert words_of(directory)["Two"] == "others~2"
        assert mira["name"] == "Mira" and mira["provenance"] == "user"
        assert "Mira" in visible(root).speaker_names
        return
    assert {key: value for key, value in mira.items() if key != "colourIndex"} == dict(
        id="others~2", name=None, provenance="unnamed", voiceSource=stamp(sha, turns)
    )
    assert "Mira" not in visible(root).speaker_names
    assert all(s["name"] != "Mira" for s in second["speakers"])
    if turns is TURNS_C:
        assert words_of(directory) == {"One": "others", "Two": "others"}
    if turns is TURNS_B:
        assert words_of(directory) == {"One": "others~2", "Two": "others~3"}
        assert speaker(second, "others~3")["name"] is None
        assert speaker(second, "others~3")["provenance"] == "unnamed"
        # others~1 no longer comes back; it is reset rather than left stale.
        assert speaker(second, "others~1")["voiceSource"] == stamp(sha, turns)


def test_appended_frames_change_the_rejoined_hash_and_reset_the_name(root, tmp_path):
    """§6.5: a real rejoin of the same audio plus frames at the end."""
    pytest.importorskip("numpy")
    from cassini_talk.diarization import apply_transcript
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.transcription import update_progress_metadata

    def digest(name, samples):
        path = tmp_path / name
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", ""))
            audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
        with rejoin([dict(path=str(path), sampleRate=16000, offsetMs=0)], scratch=tmp_path) as (_s, _f, sha):
            return sha

    pcm = [(i * 37) % 2000 - 1000 for i in range(32000)]
    before, after = digest("before.wav", pcm), digest("after.wav", pcm + [5] * 1600)
    assert before != after
    directory = capture(root)
    update_metadata(
        directory,
        speakers=[
            dict(id="others~1", name="Ada", provenance="user", colourIndex=0, voiceSource=stamp(before, TURNS_A)),
            dict(id="others~2", name="Mira", provenance="user", colourIndex=1, voiceSource=stamp(before, TURNS_A)),
        ],
    )
    words = [dict(w, speaker="others", _order=0) for w in WORDS]
    _same, kept = apply_transcript(words, [dict(id="others", durationMs=2000, inputSHA256=before, turns=TURNS_A)])
    assert not update_progress_metadata(directory, kept)
    _moved, voices = apply_transcript(words, [dict(id="others", durationMs=2100, inputSHA256=after, turns=TURNS_A)])
    assert update_progress_metadata(directory, voices)
    metadata = read_metadata(directory)
    assert [(s["name"], s["voiceSource"]) for s in metadata["speakers"]] == [(None, stamp(after, TURNS_A))] * 2


def test_recognised_name_and_cleared_flag_follow_the_right_voice(root, tmp_path):
    """§6.6 and §6.8 through the queue: old confirmed data does not ride on others~2."""
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    one, mira = embedding(1), embedding(0)
    first_prints = {"others~1": dict(embedding=one, seconds=8), "others~2": dict(embedding=mira, seconds=8)}
    directory = capture(root)
    run(root, directory, scripted({"others": (X, TURNS_A, False)}, prints=first_prints), store=store)
    save_name(directory, "others~1", "  ")  # Clear: never auto-name this voice.
    save_name(directory, "others~2", "Mira")
    assert store.remember(directory, "others~2", "Mira")
    assert store.meeting(directory)["speakers"]["others~2"]["match"]["state"] == "confirmed"
    interrupted(directory)

    # Run 2: "One"'s voice is now others~2 and Mira's voice is others~3.
    second_prints = {"others~2": dict(embedding=one, seconds=8), "others~3": dict(embedding=mira, seconds=8)}
    second = run(
        root, directory, scripted({"others": (Y, TURNS_B, False)}, prints=second_prints), resume=True, store=store
    )
    assert words_of(directory) == {"One": "others~2", "Two": "others~3"}
    assert speaker(second, "others~3")["name"] == "Mira"
    assert speaker(second, "others~3")["provenance"] == "recognised"
    reset = speaker(second, "others~2")
    assert reset["name"] is None and reset["provenance"] == "unnamed" and "nameCleared" not in reset
    assert "nameCleared" not in speaker(second, "others~1")
    cache = store.meeting(directory)["speakers"]
    assert cache["others~2"]["embedding"] == one and cache["others~2"]["match"]["state"] != "confirmed"
    assert cache["others~2"]["voiceSource"] == cache["others~3"]["voiceSource"] == stamp(Y, TURNS_B)
    assert cache["others~3"]["match"]["state"] == "auto"


def test_merges_of_reset_voices_are_pruned_and_other_groups_keep_names(root):
    """§6.7 and §6.9: only the `others` input changes; track-tab7 is reused."""
    directory = capture(root, tab=True)
    tab_words = [dict(text="Three", startMs=100, endMs=800), dict(text="Four", startMs=1100, endMs=1900)]
    words = {"others": WORDS, "track-tab7": tab_words}
    first = run(root, directory, scripted({"others": (X, TURNS_A, False), "track-tab7": (X, TURNS_A, False)}, words))
    assert speaker(first, "track-tab7~2")["voiceSource"] == stamp(X, TURNS_A)
    save_name(directory, "track-tab7~1", "Tara")
    save_name(directory, "track-tab7~2", "Theo")
    save_name(directory, "others~2", "Mira")
    merge_into(directory, "others~1", "others~2")
    merge_into(directory, "track-tab7~2", "track-tab7~1")
    interrupted(directory)

    second = run(
        root,
        directory,
        scripted({"others": (Y, TURNS_B, False), "track-tab7": (X, TURNS_A, True)}, words),
        resume=True,
    )
    assert second["speakerMerges"] == [{"from": "track-tab7~2", "into": "track-tab7~1"}]
    assert speaker(second, "track-tab7~1")["name"] == "Tara"
    assert speaker(second, "track-tab7~2")["name"] == "Theo"
    assert speaker(second, "track-tab7~1")["voiceSource"] == stamp(X, TURNS_A)
    assert speaker(second, "others~2")["name"] is None
    assert "Tara" in visible(root).speaker_names and "Mira" not in visible(root).speaker_names


def test_stampless_entries_are_reset_but_d11_names_stay(root):
    """§6.10 (D51): an entry without voiceSource counts as a mismatch."""
    directory = capture(root)
    update_metadata(
        directory,
        speakers=[
            dict(id="others", name="The room", provenance="user", colourIndex=0),
            dict(id="others~1", name=None, provenance="unnamed", colourIndex=0, nameCleared=True),
            dict(id="others~2", name="Mira", provenance="user", colourIndex=1),
        ],
    )
    metadata = run(root, directory, scripted({"others": (X, TURNS_A, False)}))
    assert speaker(metadata, "others") == dict(id="others", name="The room", provenance="user", colourIndex=0)
    for identifier in ("others~1", "others~2"):
        entry = speaker(metadata, identifier)
        assert {k: v for k, v in entry.items() if k != "colourIndex"} == dict(
            id=identifier, name=None, provenance="unnamed", voiceSource=stamp(X, TURNS_A)
        )


def test_rename_clear_and_merge_keep_the_stamp_byte_identical(root):
    """§6.11: Speakers edits never touch voiceSource."""
    directory = capture(root)
    run(root, directory, scripted({"others": (X, TURNS_A, False)}))

    def raw():
        speakers = json.loads((directory / "meeting.json").read_bytes())["speakers"]
        return {s["id"]: json.dumps(s["voiceSource"], separators=(",", ":")) for s in speakers if "voiceSource" in s}

    before = raw()
    assert set(before) == {"others~1", "others~2"}
    save_name(directory, "others~2", "Mira")
    save_name(directory, "others~1", " ")
    merge_into(directory, "others~1", "others~2")
    assert raw() == before
    assert json.dumps(stamp(X, TURNS_A), separators=(",", ":")) == before["others~2"]


@pytest.mark.parametrize("matched", [False, True])
def test_voice_cache_carries_confirmed_data_only_for_an_equal_stamp(tmp_path, matched):
    """§6.8 at the store: confirmed match, embedding and rejections."""
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    key = "Recording-12345678-1234-4234-8234-123456789abc"
    old, new = stamp(X, TURNS_A), stamp(Y, TURNS_B)
    store.commit_job(key, {"others~2": dict(embedding=embedding(0), seconds=8)}, 0, voice_sources={"others~2": old})
    store.remember(key, "others~2", "Mira")
    rejected = store.snapshot()["voices"][0]["id"]
    store.remember(key, "others~2", "Mira again")  # Rejects the first person.
    item = store.meeting(key)["speakers"]["others~2"]
    assert item["match"]["state"] == "confirmed" and item["rejected"] == [rejected]
    assert item["voiceSource"] == old
    sources = {"others~2": old if matched else new}
    store.commit_job(key, {"others~2": dict(embedding=embedding(1), seconds=9)}, 0, voice_sources=sources)
    after = store.meeting(key)["speakers"]["others~2"]
    if matched:
        assert after == item
    else:
        assert after["embedding"] == embedding(1) and after["voiceSource"] == new
        assert after["match"]["state"] != "confirmed" and "rejected" not in after


@pytest.mark.parametrize("matched", [False, True])
def test_background_preparation_labels_saved_ids_only_from_their_own_turns(root, tmp_path, monkeypatch, matched):
    """§6.12: re-inferred turns' ordinals never label saved `others~n` words."""
    pytest.importorskip("numpy")
    from cassini_talk import campplus, nemotron, worker_voices
    from cassini_talk.diarization import group_tracks
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.meeting_store import atomic_json
    from cassini_talk.transcription import prepare_tracks

    directory = capture(root)
    meeting = visible(root)
    tracks, _speakers = prepare_tracks(meeting)
    (group,) = [g for g in group_tracks(tracks) if g["id"] == "others"]
    with rejoin([t for t in tracks if t["id"] in group["tracks"]], scratch=tmp_path) as (_s, _f, sha):
        current = sha
    items = [dict(w, speaker=s) for w, s in zip(WORDS, ("others~1", "others~2"), strict=True)]
    atomic_json(directory / "transcript.json", dict(format="cassini.words.v1", language="en", items=items))
    # Saved words come from run 1 (stale cache X/A); preparation re-infers turns B.
    atomic_json(
        directory / "speaker-turns.json",
        dict(
            format=FORMAT,
            model=MODEL_TAG,
            params=PARAMS,
            groups=[dict(id="others", durationMs=2000, inputSHA256=X, turns=TURNS_A)],
        ),
    )
    source = stamp(current, TURNS_B) if matched else stamp(X, TURNS_A)
    update_metadata(
        directory,
        transcript=dict(status="done", reference=REFERENCE),
        speakers=[
            dict(id=f"others~{n}", name=None, provenance="unnamed", colourIndex=n - 1, voiceSource=source)
            for n in (1, 2)
        ],
    )
    labels = []

    class Diarizer:
        def __init__(self, *_args):
            pass

        def infer(self, _samples, _progress):
            return dict(turns=copy.deepcopy(TURNS_B))

        def close(self):
            pass

    class Extractor(Diarizer):
        def embed(self, chunks):
            return None

    real = worker_voices.select_windows

    def spy(*args, own_labels=None, **kwargs):
        labels.append(own_labels)
        return real(*args, own_labels=own_labels, **kwargs)

    monkeypatch.setattr(nemotron, "Nemotron", Diarizer)
    monkeypatch.setattr(campplus, "SpeakerExtractor", Extractor)
    monkeypatch.setattr(worker_voices, "select_windows", spy)
    monkeypatch.setattr(worker_voices.os, "nice", lambda _n: 0)
    events = []
    options = dict(
        directory=str(directory),
        prepareMissing=True,
        diarization=dict(modelPath="d", runtimePath="r"),
        modelPath="m",
        runtimePath="r",
        origin="own",
        scratch=str(tmp_path / "scratch"),
    )
    worker_voices.run(tracks, options, lambda kind, **_values: events.append(kind))
    assert events[-1] == "voices-complete"
    # TURNS_B ordinals would label others~1 as native 0 and others~2 as native 1,
    # although the saved "One" on others~1 lies in native 1's turn.
    assert labels == [{"others~1": {0}, "others~2": {1}} if matched else {}]
