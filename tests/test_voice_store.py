# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from cassini_talk import voice_store
from cassini_talk.voice_store import VoiceStore
from cassini_talk.voices_logic import REVISION, UnsupportedVersion

UUID = "12345678-1234-4234-8234-123456789abc"
RECORDING, RECEIVED = "Recording-" + UUID, "Received-" + UUID


def embedding(index=0):
    return [float(i == index) for i in range(512)]


def prints():
    return {"others~1": dict(embedding=embedding(), seconds=8)}


@pytest.fixture
def store(tmp_path):
    return VoiceStore(tmp_path / "private" / "voices")


def enrol(store, key=RECORDING, speaker="others~1", name="Alice"):
    store.refresh()
    store.commit_job(key, {speaker: prints()["others~1"]}, store.snapshot()["generation"])
    store.remember(key, speaker, name)
    return store.snapshot()["voices"][-1]


def test_folder_basename_keeps_both_origins_separate_and_survives_move(store):
    alice = enrol(store)
    store.commit_job(RECEIVED, prints(), 0)
    store.remember(RECEIVED, "others~1", "Alice", voice_id=alice["id"])
    assert {p.name for p in (store.root / "meetings").glob("*.json")} == {RECORDING + ".json", RECEIVED + ".json"}
    (voice,) = store.snapshot()["voices"]
    assert voice["meetings"] == [RECORDING, RECEIVED]
    assert voice["count"] == 2
    assert store.meeting(Path("/moved/Library") / RECORDING)["speakers"]["others~1"]["match"]["state"] == "confirmed"
    with pytest.raises(ValueError):
        store.meeting(UUID)


def test_no_private_prints_written_in_library(store, tmp_path):
    directory = tmp_path / "Library" / RECORDING
    directory.mkdir(parents=True)
    (directory / "meeting.json").write_text('{"speakers":[]}')
    before = (directory / "meeting.json").read_bytes()
    enrol(store, directory)
    assert (directory / "meeting.json").read_bytes() == before
    assert sorted(p.name for p in directory.iterdir()) == ["meeting.json"]
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / "people.json").stat().st_mode & 0o777 == 0o600


def test_confirm_is_idempotent_and_rename_takes_old_enrolment_back(store):
    alice = enrol(store)
    store.remember(RECORDING, "others~1", "Alice", voice_id=alice["id"])
    assert store.snapshot()["voices"][0]["count"] == 1
    store.remember(RECORDING, "others~1", "Bob", remember=True)
    (bob,) = store.snapshot()["voices"]
    assert bob["name"] == "Bob" and bob["count"] == 1 and bob["id"] != alice["id"]
    item = store.meeting(RECORDING)["speakers"]["others~1"]
    assert alice["id"] in item["rejected"]
    assert item["match"]["voiceId"] == bob["id"]


def test_renamed_automatic_match_is_not_suggested_again(store):
    alice = enrol(store)
    assert store.commit_job(RECEIVED, prints(), 0) == {"others~1": "Alice"}
    store.remember(RECEIVED, "others~1", "Bob", remember=False)
    assert store.commit_job(RECEIVED, prints(), 0) == {}
    assert store.snapshot()["voices"][0]["id"] == alice["id"]


def test_forget_drops_all_match_references_but_leaves_prints(store):
    alice = enrol(store)
    store.commit_job(RECEIVED, prints(), 0)
    store.forget(alice["id"])
    assert store.snapshot()["voices"] == []
    for key in (RECORDING, RECEIVED):
        item = store.meeting(key)["speakers"]["others~1"]
        assert item["match"] == dict(voiceId=None, score=None, state="none")
        assert item["embedding"] == embedding()


def test_forget_everyone_discards_inflight_jobs_and_old_meeting_cache(store):
    enrol(store)
    store.forget_everyone()
    assert store.snapshot()["generation"] == 1
    assert store.commit_job(RECORDING, prints(), 0) == {}
    assert store.meeting(RECORDING)["speakers"] == {}
    assert store.commit_job(RECORDING, prints(), 1) == {}
    assert store.meeting(RECORDING)["speakers"]


def test_interrupted_forget_everyone_cannot_revive_cache(store, monkeypatch):
    enrol(store)
    unlink = Path.unlink

    def fail(path, **kw):
        if path.parent.name == "meetings":
            raise OSError("unplugged")
        return unlink(path, **kw)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError):
        store.forget_everyone()
    restarted = VoiceStore(store.root)
    assert restarted.refresh()["voices"] == []
    assert restarted.meeting(RECORDING)["speakers"] == {}
    assert restarted.commit_job(RECEIVED, prints(), 0) == {}


def test_newer_people_store_is_readonly_and_byte_preserved(store):
    enrol(store)
    path = store.root / "people.json"
    value = json.loads(path.read_text())
    value["version"] = 2
    path.write_text(json.dumps(value))
    original = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    restarted = VoiceStore(store.root)
    assert restarted.refresh()["readOnly"]
    for operation in [
        lambda: restarted.remember(RECORDING, "others~1", "Bob"),
        lambda: restarted.forget_everyone(),
        lambda: restarted.forget(value["voices"][0]["id"]),
        lambda: restarted.commit_job(RECEIVED, prints(), 0),
    ]:
        with pytest.raises(UnsupportedVersion):
            operation()
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == original


def test_newer_meeting_cache_is_never_rewritten(store):
    enrol(store)
    path = store.root / "meetings" / (RECORDING + ".json")
    value = json.loads(path.read_text())
    value["version"] = 2
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    with pytest.raises(UnsupportedVersion):
        store.commit_job(RECORDING, prints(), 0)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "body", ["{", '{"version":1,"generation":0,"voices":[{}]}', '{"version":1,"generation":NaN,"voices":[]}']
)
def test_corruption_is_retained_and_recognition_carries_on(store, body):
    store.refresh()
    path = store.root / "people.json"
    path.write_text(body)
    assert store.refresh()["voices"] == []
    (damaged,) = store.root.glob("people.json.corrupt-*")
    assert damaged.read_text() == body
    store.commit_job(RECORDING, prints(), store.snapshot()["generation"])
    assert store.meeting(RECORDING)["speakers"]


def test_failed_multifile_write_restores_both_files(store, monkeypatch):
    enrol(store)
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    real = voice_store.atomic_json
    failed = False

    def disk_full(path, value, **kw):
        nonlocal failed
        if path.parent.name == "meetings" and not failed:
            failed = True
            raise OSError("disk full")
        real(path, value, **kw)

    monkeypatch.setattr(voice_store, "atomic_json", disk_full)
    with pytest.raises(OSError):
        store.remember(RECORDING, "others~1", "Bob")
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before
    assert VoiceStore(store.root).refresh()["voices"][0]["name"] == "Alice"


def test_interrupted_private_transaction_recovers_on_restart(store):
    alice = enrol(store)
    people = json.loads((store.root / "people.json").read_text())
    cache = json.loads((store.root / "meetings" / (RECORDING + ".json")).read_text())
    (store.root / ".transaction.json").write_text(
        json.dumps(dict(version=1, rollback={"people.json": people, "meetings/" + RECORDING + ".json": cache}))
    )
    (store.root / "people.json").write_text(json.dumps(dict(version=1, generation=0, voices=[])))
    restarted = VoiceStore(store.root)
    assert restarted.refresh()["voices"][0]["id"] == alice["id"]
    assert not (store.root / ".transaction.json").exists()


@pytest.mark.parametrize(
    "bad",
    [
        dict(embedding=embedding(), seconds=3.9),
        dict(embedding=[0.0] * 512, seconds=5),
        dict(embedding=[float("nan")] * 512, seconds=5),
    ],
)
def test_invalid_print_never_published(store, bad):
    store.refresh()
    with pytest.raises(ValueError):
        store.commit_job(RECORDING, {"others": bad}, 0)
    assert not (store.root / "meetings" / (RECORDING + ".json")).exists()


def test_stale_model_never_automatically_matches(store):
    enrol(store)
    path = store.root / "people.json"
    value = json.loads(path.read_text())
    value["voices"][0]["model"] = "0" * 64
    path.write_text(json.dumps(value))
    assert store.commit_job(RECEIVED, prints(), 0) == {}
    assert store.snapshot()["voices"][0]["model"] != REVISION


def test_recompute_replaces_only_affected_print_without_automatic_matching(store):
    alice = enrol(store)
    store.commit_job(
        RECEIVED, {"a": dict(embedding=embedding(), seconds=8), "b": dict(embedding=embedding(1), seconds=9)}, 0
    )
    store.remember(RECEIVED, "a", "Alice", voice_id=alice["id"])
    before = store.meeting(RECEIVED)["speakers"]["b"]
    assert store.recompute(
        RECEIVED,
        {"a": dict(embedding=embedding(2), seconds=10), "b": dict(embedding=embedding(3), seconds=10)},
        {"a"},
        0,
    )
    cache = store.meeting(RECEIVED)["speakers"]
    assert cache["b"] == before
    assert cache["a"]["embedding"] == embedding(2)
    assert cache["a"]["match"]["voiceId"] == alice["id"] and cache["a"]["match"]["state"] == "confirmed"
    assert store.snapshot()["voices"][0]["count"] == 2
    assert store.snapshot()["voices"][0]["seconds"] == 26  # unenrol keeps seconds


def test_recompute_failure_keeps_old_print_and_person(store, monkeypatch):
    enrol(store)
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    original = voice_store.atomic_json
    failed = False

    def fail(path, value, **kw):
        nonlocal failed
        if path.parent.name == "meetings" and not failed:
            failed = True
            raise OSError("no space")
        return original(path, value, **kw)

    monkeypatch.setattr(voice_store, "atomic_json", fail)
    with pytest.raises(OSError):
        store.recompute(RECORDING, {"others~1": dict(embedding=embedding(1), seconds=8)}, {"others~1"}, 0)
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before


def test_merge_keeps_confirmed_source_and_undo_restores_its_print(store):
    alice = enrol(store)
    source = store.meeting(RECORDING)["speakers"]["others~1"]
    target = dict(embedding=embedding(1), seconds=8)
    store.commit_job(RECORDING, {"others~1": prints()["others~1"], "others~2": target}, 0)
    assert store.recompute(RECORDING, {"others~2": target}, {"others~1", "others~2"}, 0)
    assert store.snapshot()["voices"] == [alice]
    assert store.meeting(RECORDING)["speakers"]["others~1"] == source
    assert store.recompute(
        RECORDING, {"others~1": prints()["others~1"], "others~2": target}, {"others~1", "others~2"}, 0
    )
    (restored,) = store.snapshot()["voices"]
    assert restored["id"] == alice["id"] and restored["count"] == 1
    assert restored["mean"] == embedding()
    assert store.meeting(RECORDING)["speakers"]["others~1"]["match"]["state"] == "confirmed"


def test_no_replacement_print_keeps_previous_confirmed_person_and_cache(store):
    enrol(store)
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    assert store.recompute(RECORDING, {}, {"others~1"}, 0)
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before


def test_merge_drops_automatic_source_link_and_sample_when_only_target_has_a_print(store):
    alice = enrol(store)
    source = dict(embedding=embedding(), seconds=8, windows=[dict(startMs=0, endMs=8000)])
    target = dict(embedding=embedding(1), seconds=8, windows=[dict(startMs=10000, endMs=18000)])
    assert store.commit_job(RECEIVED, {"others~1": source, "others~2": target}, 0) == {"others~1": "Alice"}
    assert set(store.snapshot()["voices"][0]["meetings"]) == {RECORDING, RECEIVED}
    assert voice_store.sample_window(store.meeting(RECEIVED), alice["id"]) == (0, 8000)

    assert store.recompute(RECEIVED, {"others~2": target}, {"others~1", "others~2"}, 0)
    assert store.meeting(RECEIVED)["speakers"] == {
        "others~2": dict(target, match=dict(voiceId=None, score=None, state="none"))
    }
    assert store.snapshot()["voices"] == [alice]
    assert not store.has_match(RECEIVED, "others~1")
    assert voice_store.sample_window(store.meeting(RECEIVED), alice["id"]) is None
    restarted = VoiceStore(store.root)
    assert restarted.refresh()["voices"] == [alice]
    assert voice_store.sample_window(restarted.meeting(RECEIVED), alice["id"]) is None


def test_undo_short_speakers_discards_combined_print_and_cannot_remember_it(store):
    store.refresh()
    store.commit_job(RECORDING, {}, 0)  # Each voice has only 2.5 seconds of clean speech.
    combined = dict(embedding=embedding(), seconds=5, windows=[dict(startMs=0, endMs=5000)])
    assert store.recompute(RECORDING, {"others~1": combined}, {"others~1", "others~2"}, 0)
    assert store.meeting(RECORDING)["speakers"]["others~1"]["seconds"] == 5

    assert store.recompute(RECORDING, {}, {"others~1", "others~2"}, 0)
    assert store.meeting(RECORDING)["speakers"] == {}
    assert not store.remember(RECORDING, "others~1", "Alice")
    assert store.snapshot()["voices"] == []
    restarted = VoiceStore(store.root)
    assert restarted.meeting(RECORDING)["speakers"] == {}
    assert not restarted.remember(RECORDING, "others~1", "Alice")


@pytest.mark.parametrize("state", ["auto", "suggested", "none", "rejected", "confirmed"])
def test_no_replacement_removes_unconfirmed_or_unknown_person_but_leaves_unaffected_print(store, state):
    alice = enrol(store)
    store.commit_job(RECEIVED, prints(), 0)
    path = store.root / "meetings" / (RECEIVED + ".json")
    value = json.loads(path.read_text())
    value["speakers"]["others~1"]["match"] = dict(
        voiceId="voice_missing" if state == "confirmed" else alice["id"], score=0.8, state=state
    )
    path.write_text(json.dumps(value))
    untouched = store.meeting(RECORDING)
    assert store.recompute(RECEIVED, {}, {"others~1"}, 0)
    assert store.meeting(RECEIVED)["speakers"] == {}
    assert store.meeting(RECORDING) == untouched
    assert store.snapshot()["voices"] == [alice]


def test_failed_pruning_preserves_automatic_print_link_and_sample(store, monkeypatch):
    alice = enrol(store)
    source = dict(embedding=embedding(), seconds=8, windows=[dict(startMs=0, endMs=8000)])
    store.commit_job(RECEIVED, {"others~1": source}, 0)
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    snapshot = store.snapshot()
    original = voice_store.atomic_json
    failed = False

    def fail(path, value, **kw):
        nonlocal failed
        if path.parent.name == "meetings" and not failed:
            failed = True
            raise OSError("no space")
        return original(path, value, **kw)

    monkeypatch.setattr(voice_store, "atomic_json", fail)
    with pytest.raises(OSError, match="no space"):
        store.recompute(RECEIVED, {}, {"others~1"}, 0)
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before
    assert store.snapshot() == snapshot
    assert voice_store.sample_window(store.meeting(RECEIVED), alice["id"]) == (0, 8000)


@pytest.mark.parametrize("affected", [("a", "b"), ("b", "a")])
def test_replacing_same_person_prints_unenrols_all_before_enrolling_any(store, affected):
    from cassini_talk.voices_logic import normalised

    store.refresh()
    store.commit_job(
        RECORDING, {"a": dict(embedding=embedding(), seconds=8), "b": dict(embedding=embedding(1), seconds=8)}, 0
    )
    store.remember(RECORDING, "a", "Alice")
    alice = store.snapshot()["voices"][0]
    store.remember(RECORDING, "b", "Alice", voice_id=alice["id"])
    replacements = {"a": dict(embedding=embedding(2), seconds=8), "b": dict(embedding=embedding(3), seconds=8)}
    assert store.recompute(RECORDING, replacements, affected, 0)
    (voice,) = store.snapshot()["voices"]
    assert voice["id"] == alice["id"] and voice["count"] == 2
    assert voice["mean"] == pytest.approx(normalised([a + b for a, b in zip(embedding(2), embedding(3), strict=True)]))


@pytest.mark.parametrize("affected", [("a", "b"), ("b", "a")])
def test_same_person_merge_and_undo_keep_both_confirmed_enrolments(store, affected):
    from cassini_talk.voices_logic import normalised

    store.refresh()
    original = {"a": dict(embedding=embedding(), seconds=8), "b": dict(embedding=embedding(1), seconds=8)}
    store.commit_job(RECORDING, original, 0)
    store.remember(RECORDING, "a", "Alice")
    alice = store.snapshot()["voices"][0]
    store.remember(RECORDING, "b", "Alice", voice_id=alice["id"])
    assert store.recompute(RECORDING, {"b": dict(embedding=embedding(2), seconds=8)}, affected, 0)
    assert store.snapshot()["voices"][0]["count"] == 2
    assert store.recompute(RECORDING, original, affected, 0)
    (voice,) = store.snapshot()["voices"]
    assert voice["id"] == alice["id"] and voice["count"] == 2
    assert voice["mean"] == pytest.approx(normalised([a + b for a, b in zip(embedding(), embedding(1), strict=True)]))


def test_forget_during_recompute_discards_result(store):
    enrol(store)
    store.forget_everyone()
    assert not store.recompute(RECORDING, prints(), {"others~1"}, 0)
    assert store.snapshot()["voices"] == [] and not store.meeting(RECORDING)["speakers"]


def test_count_is_current_auto_or_confirmed_library_meetings_not_enrolments(store):
    alice = enrol(store)
    assert store.commit_job(RECEIVED, prints(), 0) == {"others~1": "Alice"}
    (current,) = store.snapshot()["voices"]
    assert set(current["meetings"]) == {RECORDING, RECEIVED} and current["count"] == 1
    store.remember(RECEIVED, "others~1", "Someone else", remember=False)
    (current,) = store.snapshot()["voices"]
    assert current["meetings"] == [RECORDING]
    store.sync_library([])
    (current,) = store.snapshot()["voices"]
    assert current["meetings"] == [] and current["id"] == alice["id"]
    assert not store.has_match(RECORDING, "others~1")
    store.sync_library([RECORDING, RECEIVED])
    assert store.snapshot()["voices"][0]["meetings"] == [RECORDING]
    assert store.has_match(RECORDING, "others~1")


def test_sample_uses_only_auto_and_confirmed_voice_links():
    from cassini_talk.voice_store import sample_window

    cache = dict(
        speakers={
            state: dict(match=dict(voiceId="alice", state=state), windows=[dict(startMs=start, endMs=end)])
            for state, start, end in [("confirmed", 0, 5000), ("suggested", 10000, 20000), ("rejected", 30000, 40000)]
        }
    )
    assert sample_window(cache, "alice") == (0, 5000)
    assert sample_window(cache, "unknown") is None


def test_recompute_keeps_automatic_link_and_rejections_without_rematching(store):
    alice = enrol(store)
    store.commit_job(RECEIVED, prints(), 0)
    path = store.root / "meetings" / (RECEIVED + ".json")
    value = json.loads(path.read_text())
    value["speakers"]["others~1"]["rejected"] = ["voice_rejected"]
    path.write_text(json.dumps(value))
    assert store.recompute(RECEIVED, {"others~1": dict(embedding=embedding(1), seconds=9)}, {"others~1"}, 0)
    item = store.meeting(RECEIVED)["speakers"]["others~1"]
    assert item["match"] == dict(voiceId=alice["id"], state="auto", score=0.0)
    assert item["rejected"] == ["voice_rejected"]
    assert store.has_match(RECEIVED, "others~1")
    assert set(store.snapshot()["voices"][0]["meetings"]) == {RECORDING, RECEIVED}


def test_protected_names_only_get_suggestions_and_do_not_take_automatic_match_slots(store):
    from cassini_talk.voice_store import sample_window

    alice = enrol(store)
    protected = dict(embedding=embedding(), seconds=8, windows=[dict(startMs=0, endMs=8000)])
    other = dict(embedding=embedding(), seconds=8, windows=[dict(startMs=10000, endMs=18000)])
    names = store.commit_job(RECEIVED, {"cleared": protected, "other": other}, 0, auto_eligible=lambda: {"other"})
    cache = store.meeting(RECEIVED)
    assert names == {"other": "Alice"}
    assert cache["speakers"]["cleared"]["match"]["state"] == "suggested"
    assert not store.has_match(RECEIVED, "cleared") and store.has_match(RECEIVED, "other")
    assert sample_window(cache, alice["id"]) == (10000, 18000)
    store.commit_job(RECEIVED, {"cleared": protected}, 0, auto_eligible=lambda: set())
    assert store.snapshot()["voices"][0]["meetings"] == [RECORDING]
    assert sample_window(store.meeting(RECEIVED), alice["id"]) is None


@pytest.mark.parametrize("operation", ["job", "remember", "recompute"])
def test_current_meeting_updates_link_before_library_watcher_catches_up(store, operation):
    alice = enrol(store)
    if operation != "job":
        store.commit_job(RECEIVED, prints(), 0)
        if operation == "recompute":
            store.remember(RECEIVED, "others~1", "Alice", voice_id=alice["id"])
    store.sync_library([RECORDING])
    assert not store.has_match(RECEIVED, "others~1")
    if operation == "job":
        store.commit_job(RECEIVED, prints(), 0)
    elif operation == "remember":
        store.remember(RECEIVED, "others~1", "Alice", voice_id=alice["id"])
    else:
        store.recompute(RECEIVED, prints(), {"others~1"}, 0)
    assert store.has_match(RECEIVED, "others~1")
    assert set(store.snapshot()["voices"][0]["meetings"]) == {RECORDING, RECEIVED}


def test_undivided_me_never_becomes_a_saved_voice(store):
    store.refresh()
    with pytest.raises(ValueError):
        store.commit_job(RECORDING, {"me": dict(embedding=embedding(), seconds=8)}, 0)
    assert store.snapshot()["voices"] == []


def test_preparation_distinguishes_missing_damaged_and_successfully_short(store):
    cache, prepared = store.preparation(RECORDING)
    assert not prepared and not cache["speakers"]
    store.commit_job(RECORDING, {}, 0)  # Successful extraction, under 4 s.
    cache, prepared = store.preparation(RECORDING)
    assert prepared and not cache["speakers"]
    path = store.root / "meetings" / (RECORDING + ".json")
    path.write_text("{")
    assert not store.preparation(RECORDING)[1]
    assert list(path.parent.glob(path.name + ".corrupt-*"))


def test_job_retry_preserves_confirmed_print_and_does_not_count_it_twice(store):
    alice = enrol(store)
    original = store.meeting(RECORDING)["speakers"]["others~1"]
    store.commit_job(
        RECORDING,
        {"others~1": dict(embedding=embedding(1), seconds=9), "another": dict(embedding=embedding(), seconds=8)},
        0,
    )
    assert store.meeting(RECORDING)["speakers"]["others~1"] == original
    assert store.snapshot()["voices"][0]["count"] == 1
    assert store.snapshot()["voices"][0]["id"] == alice["id"]
    assert store.meeting(RECORDING)["speakers"]["another"]["match"]["state"] == "none"


def test_recovery_journal_can_exceed_one_file_limit(store, monkeypatch):
    enrol(store)
    old = {str(p.relative_to(store.root)): json.loads(p.read_text()) for p in store.root.rglob("*.json")}
    assert all(p.stat().st_size < 4096 for p in store.root.rglob("*.json"))
    journal = store.root / ".transaction.json"
    journal.write_text(json.dumps(dict(version=1, rollback=old)))
    assert journal.stat().st_size > 4096
    (store.root / "people.json").write_text(json.dumps(dict(version=1, generation=0, voices=[])))
    monkeypatch.setattr(voice_store, "LIMIT", 4096)
    assert VoiceStore(store.root).refresh()["voices"][0]["name"] == "Alice"
    assert not journal.exists()


def test_edit_never_installs_a_journal_it_cannot_recover(store, monkeypatch):
    enrol(store)
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    monkeypatch.setattr(voice_store, "JOURNAL_LIMIT", 128)
    with pytest.raises(ValueError, match="recovery size limit"):
        store.remember(RECORDING, "others~1", "Bob")
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before


def test_trash_removes_only_one_meetings_prints_without_unenrolment(store):
    alice = enrol(store)
    store.commit_job(RECEIVED, prints(), 0)
    store.remember(RECEIVED, "others~1", "Alice", voice_id=alice["id"])
    before = json.loads((store.root / "people.json").read_text())["voices"][0]
    other_bytes = (store.root / "meetings" / (RECEIVED + ".json")).read_bytes()
    store.remove_meeting(RECORDING)
    assert not (store.root / "meetings" / (RECORDING + ".json")).exists()
    assert (store.root / "meetings" / (RECEIVED + ".json")).read_bytes() == other_bytes
    after = json.loads((store.root / "people.json").read_text())["voices"][0]
    assert after["meetings"] == [RECEIVED]
    assert {k: v for k, v in after.items() if k != "meetings"} == {k: v for k, v in before.items() if k != "meetings"}
    assert store.snapshot()["voices"][0]["name"] == "Alice"
    store.remove_meeting(RECORDING)  # A repeated notification remains harmless.


def test_trash_cache_transaction_recovers_deletion_if_people_write_fails(store, monkeypatch):
    enrol(store)
    before = {p: p.read_bytes() for p in [store.root / "people.json", store.root / "meetings" / (RECORDING + ".json")]}
    original = voice_store.atomic_json
    failed = False

    def fail_once(path, value, **kwargs):
        nonlocal failed
        if path.name == "people.json" and not failed:
            failed = True
            raise OSError("generated write failure")
        return original(path, value, **kwargs)

    monkeypatch.setattr(voice_store, "atomic_json", fail_once)
    with pytest.raises(OSError):
        store.remove_meeting(RECORDING)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not (store.root / ".transaction.json").exists()
