# SPDX-License-Identifier: GPL-3.0-or-later
"""D-977 guided test-phrase enrollment, rename and one-question voice setup (no GTK, no engine)."""

import json
import math
import os
import re
import struct
import sys
import threading
import time
from array import array
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_voice_store import RECORDING, embedding, enrol

from cassini_talk import voice_enroll
from cassini_talk.voice_enroll import (
    FRAME,
    MAX_ENROL_MS,
    MIN_ENROL_MS,
    RATE,
    VoiceSetup,
    enrol_windows,
    judge,
    private_sample,
    speech_ms,
    speech_segments,
    sweep,
    voice_setup_step,
)
from cassini_talk.voice_store import VoiceStore
from cassini_talk.voices_logic import EDGE_TRIM_MS, MAX_WINDOW_MS, REVISION

ROOT = Path(__file__).parents[1]


def tone(ms, amplitude=0.1):
    return [amplitude * math.sin(2 * math.pi * 220 * i / RATE) for i in range(ms * RATE // 1000)]


def silence(ms):
    return [0.0] * (ms * RATE // 1000)


@pytest.fixture
def store(tmp_path):
    return VoiceStore(tmp_path / "private" / "voices")


# Speech measure (>= 4 s of clear speech, D25 MIN_REMEMBER_MS).


def test_silence_and_clicks_are_not_speech():
    assert speech_segments(silence(3000)) == []
    clicks = silence(500) + tone(64) + silence(1000) + tone(64) + silence(500)  # 2-frame clicks
    assert speech_segments(clicks) == []


def test_short_pauses_join_and_long_pauses_split():
    joined = speech_segments(tone(2000) + silence(300) + tone(2000))
    assert len(joined) == 1
    split = speech_segments(tone(2000) + silence(1000) + tone(2000))
    assert len(split) == 2
    assert speech_ms(split) == pytest.approx(4000, abs=2 * 1000 * FRAME // RATE)


def test_four_seconds_after_trim_is_the_bar():
    windows, ok = judge(silence(500) + tone(3500) + silence(500))
    assert not ok
    windows, ok = judge(silence(500) + tone(MIN_ENROL_MS + 2 * EDGE_TRIM_MS + 200) + silence(500))
    assert ok and len(windows) == 1
    start, end = windows[0]
    assert (end - start) * 1000 // RATE >= MIN_ENROL_MS
    assert start >= 500 * RATE // 1000 + EDGE_TRIM_MS * RATE // 1000 - FRAME


def test_long_takes_split_into_ten_second_windows_in_order():
    windows = enrol_windows([(0, 25 * RATE)])
    assert len(windows) == 3
    assert all(end - start <= MAX_WINDOW_MS * RATE // 1000 for start, end in windows)
    assert windows == sorted(windows)
    assert all(a[1] <= b[0] for a, b in zip(windows, windows[1:], strict=False))


def test_quiet_room_is_too_short_even_when_long():
    assert not judge(tone(8000, amplitude=0.001))[1]  # -63 dBFS: below the -50 dBFS speech rule


def test_speech_rule_matches_the_test_recording_meter():
    sound_check = pytest.importorskip("cassini_talk.sound_check", exc_type=ImportError)
    assert (voice_enroll.ACTIVE_DBFS, voice_enroll.FRAME_MS, voice_enroll.MIN_RUN_FRAMES) == (
        sound_check.ACTIVE_DBFS,
        sound_check.FRAME_MS,
        sound_check.MIN_RUN_FRAMES,
    )


def test_speech_measure_is_linear_in_cpu_time():
    """Bug class 1/17: bounded by scaling of thread CPU time, never by wall clock."""
    pattern = tone(1500) + silence(600)
    short, long = pattern * 4, pattern * 16

    def cost(samples):
        best = math.inf
        for _ in range(3):
            started = time.thread_time()
            judge(samples)
            best = min(best, time.thread_time() - started)
        return best

    small, large = cost(short), cost(long)
    # 4x the input should cost about 4x; 8x leaves room for noise, still far from quadratic (16x).
    assert large <= max(small, 1e-3) * 8


# Private sample file.


def test_private_sample_is_0600_little_endian_and_always_deleted(tmp_path):
    root = tmp_path / "cassini-talk" / "enrol"
    with private_sample([0.5, -0.25], root) as path:
        assert path.parent == root
        assert path.stat().st_mode & 0o777 == 0o600
        assert root.stat().st_mode & 0o777 == 0o700
        assert struct.unpack("<2f", path.read_bytes()) == (0.5, -0.25)
    assert not path.exists()
    with pytest.raises(RuntimeError), private_sample([0.1], root) as path:
        raise RuntimeError("worker failed")
    assert not path.exists() and list(root.iterdir()) == []


def test_private_sample_refuses_a_linked_folder(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "cassini-talk").mkdir()
    (tmp_path / "cassini-talk" / "enrol").symlink_to(real)
    with pytest.raises(ValueError), private_sample([0.1], tmp_path / "cassini-talk" / "enrol"):
        pass
    assert list(real.iterdir()) == []


def test_sweep_deletes_only_left_behind_samples(tmp_path):
    root = tmp_path / "enrol"
    root.mkdir()
    stale = root / f"{uuid4()}.f32"
    stale.write_bytes(b"\0" * 8)
    keep = [root / "notes.txt", root / "not-a-uuid.f32"]
    for path in keep:
        path.write_text("x")
    sweep(root)
    assert not stale.exists() and all(path.exists() for path in keep)


def test_sample_folder_is_private_cache_not_library_or_data(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert voice_enroll.samples_root() == tmp_path / "cache" / "cassini-talk" / "enrol"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")
    assert voice_enroll.samples_root() == Path.home() / ".cache" / "cassini-talk" / "enrol"


def test_extract_print_deletes_the_take_and_passes_only_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    seen = {}

    class Client:
        def __init__(self, bundle, model, runtime, timeout):
            seen["model"] = model

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def enrol_sample(self, path, frames, windows, options):
            seen.update(path=path, exists=path.exists(), frames=frames, windows=windows)
            return dict(embedding=embedding(), seconds=5.0)

    import cassini_talk.transcription as transcription
    import cassini_talk.voice_models as voice_models

    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    monkeypatch.setattr(transcription, "bundle_path", lambda: tmp_path)
    models = SimpleNamespace(request=lambda directory, origin: dict(modelPath="m", runtimePath="r"))
    result = voice_enroll.extract_print(SimpleNamespace(voice_models=models), [0.0] * 100, [(0, 50)])
    assert result["seconds"] == 5.0
    assert seen["exists"] and not seen["path"].exists()
    assert seen["frames"] == 100 and seen["windows"] == [(0, 50)]
    off = SimpleNamespace(request=lambda directory, origin: None)
    assert voice_enroll.extract_print(SimpleNamespace(voice_models=off), [0.0], [(0, 1)]) is None
    assert voice_enroll.extract_print(SimpleNamespace(voice_models=None), [0.0], [(0, 1)]) is None


# Store: guided samples and rename.


def test_guided_sample_makes_a_new_voice_with_the_next_save_colour(store):
    alice = enrol(store)
    identifier = store.add_sample(" Bea ", embedding(1), 6.5, store.snapshot()["generation"])
    voices = {v["id"]: v for v in store.snapshot()["voices"]}
    bea = voices[identifier]
    assert bea["name"] == "Bea" and bea["count"] == 1 and bea["seconds"] == 6.5
    assert bea["meetings"] == [] and bea["model"] == REVISION
    assert bea["saveOrder"] == voices[alice["id"]]["saveOrder"] + 1
    # A voice with no meeting behind it survives Library syncs and restarts.
    store.sync_library({RECORDING})
    again = VoiceStore(store.root)
    again.refresh()
    assert any(v["id"] == identifier and v["meetings"] == [] for v in again.snapshot()["voices"])


def test_guided_sample_adds_to_an_existing_voice_as_same_person(store):
    alice = enrol(store)
    store.add_sample("Alice", embedding(0), 5, store.snapshot()["generation"], voice_id=alice["id"])
    (voice,) = store.snapshot()["voices"]
    assert voice["id"] == alice["id"] and voice["count"] == 2
    assert voice["saveOrder"] == alice["saveOrder"] and voice["meetings"] == [RECORDING]
    assert voice["seconds"] == alice["seconds"] + 5


def test_forget_everyone_or_forget_during_a_take_discards_it(store):
    alice = enrol(store)
    generation = store.snapshot()["generation"]
    store.forget(alice["id"])
    assert store.add_sample("Alice", embedding(), 5, generation, voice_id=alice["id"]) is None
    assert store.snapshot()["voices"] == []
    store.forget_everyone()
    assert store.add_sample("Bea", embedding(), 5, generation) is None
    assert store.snapshot()["voices"] == []


@pytest.mark.parametrize(
    ("name", "seconds"), [("", 5), ("   ", 5), ("x" * 65, 5), ("Bea", 3.9), ("Bea", math.nan), ("Bea", "5")]
)
def test_guided_sample_needs_a_name_and_four_seconds(store, name, seconds):
    store.refresh()
    with pytest.raises(ValueError):
        store.add_sample(name, embedding(), seconds, 0)
    assert store.snapshot()["voices"] == []


def test_guided_sample_writes_only_the_private_store(store, tmp_path):
    library = tmp_path / "Library" / RECORDING
    library.mkdir(parents=True)
    (library / "meeting.json").write_text('{"speakers":[]}')
    before = {p: p.read_bytes() for p in library.iterdir()}
    store.refresh()
    store.add_sample("Bea", embedding(), 5, 0)
    assert {p: p.read_bytes() for p in library.iterdir()} == before
    assert (store.root / "people.json").stat().st_mode & 0o777 == 0o600
    assert {p.name for p in store.root.iterdir()} <= {"people.json", "meetings", ".lock"}


def test_rename_changes_only_the_saved_voice(store):
    alice = enrol(store)
    cache = json.dumps(store.meeting(RECORDING), sort_keys=True)
    assert store.rename(alice["id"], "  Alice Rossi ")
    (voice,) = store.snapshot()["voices"]
    assert voice["name"] == "Alice Rossi" and voice["mean"] == alice["mean"] and voice["count"] == alice["count"]
    assert voice["saveOrder"] == alice["saveOrder"]
    assert json.dumps(store.meeting(RECORDING), sort_keys=True) == cache
    assert not store.rename(alice["id"], "Alice Rossi")
    again = VoiceStore(store.root)
    again.refresh()
    assert again.snapshot()["voices"][0]["name"] == "Alice Rossi"


def test_rename_rejects_blank_and_unknown(store):
    alice = enrol(store)
    for bad in ("", "  ", "x" * 65, "a​b"):
        with pytest.raises(ValueError):
            store.rename(alice["id"], bad)
    with pytest.raises(KeyError):
        store.rename("voice_missing", "Bob")
    assert store.snapshot()["voices"][0]["name"] == "Alice"


def test_rename_and_guided_sample_never_touch_a_newer_store(store):
    store.root.mkdir(parents=True)
    (store.root / "people.json").write_text(json.dumps(dict(version=2, generation=0, voices=[])))
    before = (store.root / "people.json").read_bytes()
    with pytest.raises(ValueError):
        store.add_sample("Bea", embedding(), 5, 0)
    with pytest.raises(ValueError):
        store.rename("voice_x", "Bea")
    assert (store.root / "people.json").read_bytes() == before


# Worker child and client (fake extractor; the real one runs in tests/voices).


@pytest.fixture
def fake_extractor(monkeypatch):
    np = pytest.importorskip("numpy")
    calls = []

    class Extractor:
        def __init__(self, model, runtime):
            calls.append(("open", model, runtime))

        def embed(self, chunks):
            calls.append(("embed", [len(c) for c in chunks], [float(np.asarray(c)[0]) for c in chunks]))
            frames = sum(len(c) for c in chunks)
            if frames < 4 * 16000:
                return None
            return dict(embedding=embedding(), seconds=frames / 16000)

        def close(self):
            calls.append(("close",))

    import cassini_talk.campplus as campplus

    monkeypatch.setattr(campplus, "SpeakerExtractor", Extractor)
    return calls


def write_take(path, frames):
    data = array("f", [float(i // 16000) for i in range(frames)])
    if sys.byteorder != "little":
        data.byteswap()
    path.write_bytes(data.tobytes())


def run_child(message):
    from cassini_talk.worker_voices import enrol_sample

    out = []
    enrol_sample(message, lambda kind, **values: out.append(dict(type=kind, **values)))
    return out


def test_child_embeds_only_the_chosen_windows(tmp_path, fake_extractor):
    take = tmp_path / "take.f32"
    write_take(take, 6 * 16000)
    options = dict(modelPath="m", runtimePath="r")
    (reply,) = run_child(
        dict(path=str(take), frames=6 * 16000, windows=[[16000, 48000], [64000, 96000]], options=options)
    )
    assert reply["type"] == "enrol-print" and reply["seconds"] == 4
    assert ("embed", [32000, 32000], [1.0, 4.0]) in fake_extractor and fake_extractor[-1] == ("close",)
    (short,) = run_child(dict(path=str(take), frames=6 * 16000, windows=[[0, 16000]], options=options))
    assert short == dict(type="enrol-too-short")


@pytest.mark.parametrize(
    "windows",
    [[], [[0]], [[10, 5]], [[0, 96001]], [[0, 170000]], [[32000, 48000], [16000, 20000]], [[0.5, 100]], "x"],
)
def test_child_rejects_bad_windows(tmp_path, fake_extractor, windows):
    take = tmp_path / "take.f32"
    write_take(take, 6 * 16000)
    (reply,) = run_child(dict(path=str(take), frames=6 * 16000, windows=windows, options={}))
    assert reply["type"] == "voices-error"


def test_child_rejects_a_changed_or_linked_take(tmp_path, fake_extractor):
    take = tmp_path / "take.f32"
    write_take(take, 5 * 16000)
    (reply,) = run_child(dict(path=str(take), frames=6 * 16000, windows=[[0, 16000]], options={}))
    assert reply["type"] == "voices-error"
    link = tmp_path / "link.f32"
    link.symlink_to(take)
    (reply,) = run_child(dict(path=str(link), frames=5 * 16000, windows=[[0, 16000]], options={}))
    assert reply["type"] == "voices-error"


def client_with(replies):
    from cassini_talk.voice_models import VoiceJobClient

    client = object.__new__(VoiceJobClient)
    written = []
    client.process = SimpleNamespace(stdin=SimpleNamespace(write=written.append, flush=lambda: None))
    queue = list(replies)
    client._receive = lambda expected=None: queue.pop(0)
    return client, written


def test_client_sends_one_bounded_request_and_validates_the_print():
    client, written = client_with([dict(type="enrol-print", embedding=embedding(), seconds=5)])
    result = client.enrol_sample(Path("/private/take.f32"), 80000, [(0, 80000)], dict(modelPath="m", runtimePath="r"))
    assert result == dict(embedding=embedding(), seconds=5.0)
    (packet,) = written
    message = json.loads(packet)
    assert message == dict(
        type="enrolSample",
        path="/private/take.f32",
        frames=80000,
        windows=[[0, 80000]],
        options=dict(modelPath="m", runtimePath="r"),
    )
    client, _ = client_with([dict(type="enrol-too-short")])
    assert client.enrol_sample(Path("/t"), 1, [(0, 1)], dict(modelPath="m", runtimePath="r")) is None


@pytest.mark.parametrize(
    "reply",
    [
        dict(type="voices-error", message="boom"),
        dict(type="enrol-print", embedding=[0.0] * 3, seconds=5),
        dict(type="enrol-print", embedding=embedding()[:-1] + [math.inf], seconds=5),
        dict(type="enrol-print", embedding=embedding(), seconds=0),
    ],
)
def test_client_refuses_bad_replies(reply):
    client, _ = client_with([reply])
    with pytest.raises((RuntimeError, ValueError)):
        client.enrol_sample(Path("/t"), 1, [(0, 1)], dict(modelPath="m", runtimePath="r"))


# One-question voice setup.

DOWNLOADED_ON = dict(downloaded=True, ready=True, busy=False, downloading=False, error=False)
MISSING = dict(downloaded=False, ready=False, busy=False, downloading=False, error=False)


@pytest.mark.parametrize(
    ("tell", "voices", "step"),
    [
        (MISSING, MISSING, "download-tell"),
        (dict(MISSING, downloading=True), MISSING, "wait"),
        (dict(MISSING, busy=True), MISSING, "wait"),
        (dict(MISSING, error=True), MISSING, "wait"),
        (dict(DOWNLOADED_ON, ready=False), MISSING, "wait"),
        (DOWNLOADED_ON, MISSING, "download-voices"),
        (DOWNLOADED_ON, dict(MISSING, busy=True), "wait"),
        (DOWNLOADED_ON, dict(MISSING, error=True), "wait"),
        (DOWNLOADED_ON, dict(MISSING, downloading=True), "done"),
        (DOWNLOADED_ON, DOWNLOADED_ON, "done"),
    ],
)
def test_remember_voices_downloads_tell_apart_first_then_recognition(tell, voices, step):
    assert voice_setup_step(tell, voices) == step


class FakeModels:
    """SpeechModels' download contract: busy while running, Cancel clears it without an error."""

    def __init__(self, **state):
        self.state = dict(MISSING, **state)
        self.downloads = 0

    def snapshot(self):
        return dict(self.state)

    def download(self):
        if self.state["busy"]:
            return False
        self.downloads += 1
        self.state.update(busy=True, downloading=True, error=False)
        return True

    def cancel_download(self):
        self.state.update(busy=False, downloading=False)  # Cancellation sets no error.

    def remove(self):
        self.state.update(downloaded=False, ready=False, busy=False)

    def finish(self):
        self.state.update(busy=False, downloading=False, downloaded=True, ready=True)


def notify(setup, tell, voices, times=3):
    """`_models_changed` runs after every model change, often more than once."""
    for _ in range(times):
        setup.advance(tell, voices)


def test_cancelling_tell_apart_during_setup_never_restarts_it():
    setup, tell, voices = VoiceSetup(), FakeModels(), FakeModels()
    setup.begin(tell, voices)
    assert tell.downloads == 1 and setup.active
    notify(setup, tell, voices)  # Progress notifications while it downloads.
    assert tell.downloads == 1 and setup.active
    tell.cancel_download()
    notify(setup, tell, voices)
    assert (tell.downloads, voices.downloads, setup.active) == (1, 0, False)


def test_cancelling_recognition_during_setup_never_restarts_or_chains():
    setup, tell, voices = VoiceSetup(), FakeModels(), FakeModels()
    setup.begin(tell, voices)
    tell.finish()
    notify(setup, tell, voices)
    assert (tell.downloads, voices.downloads, setup.active) == (1, 1, False)
    voices.cancel_download()
    notify(setup, tell, voices)
    assert (tell.downloads, voices.downloads, setup.active) == (1, 1, False)


def test_removing_tell_apart_during_setup_ends_the_request():
    setup, tell, voices = VoiceSetup(), FakeModels(), FakeModels()
    setup.begin(tell, voices)
    tell.state.update(busy=False, downloading=False, downloaded=True)  # Checking the runtime.
    notify(setup, tell, voices)
    tell.remove()
    notify(setup, tell, voices)
    assert (tell.downloads, voices.downloads, setup.active) == (1, 0, False)


def test_a_failed_setup_download_waits_for_try_again_and_then_chains():
    setup, tell, voices = VoiceSetup(), FakeModels(), FakeModels()
    setup.begin(tell, voices)
    tell.state.update(busy=False, downloading=False, error=True)
    notify(setup, tell, voices)
    assert (tell.downloads, setup.active) == (1, True)
    tell.download()  # Try again on the Settings row.
    tell.finish()
    notify(setup, tell, voices)
    assert (tell.downloads, voices.downloads, setup.active) == (2, 1, False)


def settings_dialog_class():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("GTK introspection is unavailable")
    try:
        from cassini_talk.settings_dialog import SettingsDialog
    except (ImportError, ValueError) as error:
        pytest.skip(f"Settings dialog is unavailable: {error}")
    return SettingsDialog


@pytest.mark.parametrize("row", ["tell", "voices"])
def test_settings_cancel_and_remove_end_remember_voices(row):
    from functools import partial

    dialog_class = settings_dialog_class()
    for action in ("cancel", "remove"):
        setup, tell, voices = VoiceSetup(), FakeModels(), FakeModels()
        setup.begin(tell, voices)
        if row == "voices":
            tell.finish()
            setup.advance(tell, voices)  # Chains Recognise saved voices.
            setup.active = True  # Ending is the dialog's job here, whatever the chain did.
        models = tell if row == "tell" else voices
        if action == "remove":
            models.finish()
        dialog = SimpleNamespace(
            app=SimpleNamespace(diarization_models=tell, voice_models=voices, voice_setup=setup),
            render_diarization=lambda: None,
            render_voices=lambda: None,
        )
        dialog._remove_optional = partial(dialog_class._remove_optional, dialog)
        (dialog_class._nemotron_action if row == "tell" else dialog_class._voice_action)(dialog)
        assert not setup.active, action
        assert not models.state["downloading"] and not models.state["downloaded"], action
        notify(setup, tell, voices)
        assert tell.downloads + voices.downloads == 1 + (row == "voices"), action


def test_voice_setup_answer_is_a_settings_key_defaulting_to_unanswered():
    schema = (ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml").read_text()
    block = re.search(r'<key name="voice-setup" type="s">(.*?)</key>', schema, re.S).group(1)
    assert "<default>'unanswered'</default>" in block
    assert re.findall(r'<choice value="([^"]+)"', block) == ["unanswered", "enabled", "declined"]


# Copy: Wave 6 catalog ids exist, and the GTK process never imports the engine.

WAVE6_IDS = [
    "voices.setup.title",
    "voices.setup.body",
    "voices.setup.privacy.desktop",
    "voices.setup.enable",
    "common.notNow",
    "voices.enroll.addAction",
    "voices.enroll.title",
    "voices.enroll.instructions",
    "voices.enroll.testPhraseLabel",
    "voices.enroll.testPhrase",
    "voices.enroll.record",
    "voices.enroll.recording",
    "voices.enroll.tooShort",
    "voices.enroll.ready",
    "voices.enroll.save",
    "voices.enroll.fromTranscriptHint",
    "savedVoices.privacy",
    "savedVoices.row.meta",
    "savedVoices.row.sampleCount",
    "savedVoices.row.status.ready",
    "savedVoices.row.stale",
    "savedVoices.row.play",
    "savedVoices.row.stopSample",
    "savedVoices.row.rename",
    "savedVoices.rename.title",
    "savedVoices.rename.save",
    "savedVoices.rename.blank",
    "savedVoices.row.delete",
    "savedVoices.forgetEveryone",
    "confirm.deleteVoiceProfile.title",
    "confirm.deleteVoiceProfile.body",
    "confirm.deleteVoiceProfile.primary",
    "confirm.forgetEveryone.title",
    "confirm.forgetEveryone.body",
    "confirm.forgetEveryone.primary",
]


def test_every_wave6_id_is_vendored_for_gnome_and_used():
    entries = {e["id"]: e for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_text())["entries"]}
    sources = "".join(
        (ROOT / "src/cassini_talk" / name).read_text() for name in ("saved_voices_page.py", "voice_enroll_dialog.py")
    )
    for identifier in WAVE6_IDS:
        assert "gnome" in entries[identifier]["platforms"], identifier
        prefix = identifier.rsplit(".", 1)[0]
        assert f'"{identifier}"' in sources or (
            identifier.startswith("confirm.") and 'prefix + ".' in sources and f'"{prefix}"' in sources
        ), identifier
    # Phone-only privacy copy is never drawn on the desktop (HARMONY §12).
    assert "privacy.phone" not in sources and "savedVoices.privacy.mac" not in sources


def test_desktop_privacy_copy_claims_no_upload_and_no_export():
    entries = {e["id"]: e["en"] for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_text())["entries"]}
    for identifier in ("savedVoices.privacy", "voices.setup.privacy.desktop"):
        assert "never uploaded" in entries[identifier] and "exported" in entries[identifier]
        assert "backup" not in entries[identifier].lower() and "iCloud" not in entries[identifier]


def test_enrollment_modules_import_neither_numpy_nor_the_engine():
    for name in ("voice_enroll.py", "phrase_recorder.py", "voice_enroll_dialog.py", "saved_voices_page.py"):
        source = (ROOT / "src/cassini_talk" / name).read_text()
        assert not re.search(r"^\s*(import numpy|from \.campplus|import cassini_talk\.campplus)", source, re.M), name
    assert "socket" not in (ROOT / "src/cassini_talk/voice_enroll.py").read_text()


def test_voice_sample_take_bound_fits_the_child_budget():
    from cassini_talk.voices_logic import MAX_SECONDS

    assert MAX_ENROL_MS <= MAX_SECONDS * 1000
    assert os.path.basename(str(voice_enroll.samples_root())) == "enrol"


# The microphone take (audiotestsrc stands in for pulsesrc).


def run_recorder(source, *, stop_after_level=False):
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer introspection is unavailable")
    from gi.repository import GLib, Gst

    Gst.init(None)
    if Gst.ElementFactory.find("audiotestsrc") is None or Gst.ElementFactory.find("appsink") is None:
        pytest.skip("audiotestsrc/appsink are unavailable")
    from cassini_talk.phrase_recorder import PhraseRecorder

    recorder = PhraseRecorder(source)
    done, levels = [], []
    loop = GLib.MainLoop()

    def finished(_recorder, ok):
        done.append(ok)
        loop.quit()

    def level(_recorder, dbfs):
        levels.append(dbfs)
        if stop_after_level:
            recorder.stop()

    recorder.connect("finished", finished)
    recorder.connect("level", level)
    recorder.start()
    guard = GLib.timeout_add_seconds(20, loop.quit)  # Hang guard only; nothing asserts elapsed time.
    loop.run()
    GLib.source_remove(guard) if done else None
    return recorder, done, levels


def test_recorder_keeps_every_chunk_until_end_of_stream(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recorder, done, levels = run_recorder(
        "audiotestsrc num-buffers=10 samplesperbuffer=1600 wave=sine volume=0.2 ! audio/x-raw,rate=16000"
    )
    assert done == [True] and levels
    assert len(recorder.samples) == 16000  # EOS never overtakes the last chunk.
    assert not recorder.listening and list(tmp_path.iterdir()) == []


def test_recorder_stops_itself_at_the_take_bound():
    recorder, done, _ = run_recorder("audiotestsrc is-live=false samplesperbuffer=16000 ! audio/x-raw,rate=16000")
    assert done == [True] and len(recorder.samples) == MAX_ENROL_MS * RATE // 1000


def test_recorder_reports_a_microphone_that_cannot_open():
    recorder, done, _ = run_recorder("audiotestsrc ! capsfilter caps=audio/x-raw,format=S8,rate=1 ! fakeelement")
    assert done == [False] and not recorder.listening


def test_cancel_drops_the_take():
    recorder, done, _ = run_recorder(
        "audiotestsrc samplesperbuffer=1600 ! audio/x-raw,rate=16000", stop_after_level=True
    )
    assert done == [True] and len(recorder.samples) > 0
    recorder.cancel()
    assert len(recorder.samples) == 0


class BlockedStateChange:
    """Holds one pipeline state change on its calling thread until `release` is set.

    Stands in for a sound server that accepts the connection but never answers.
    Records which thread asked for every state change, queues `on_enter` on the
    main loop from the held worker, and sets `entered` / `closed` so the test is
    gated on events rather than elapsed time.
    """

    def __init__(self, monkeypatch, GLib, Gst, blocked, on_enter=None):
        self.release, self.entered, self.closed = threading.Event(), threading.Event(), threading.Event()
        self.calls, self.pipelines = [], []
        real, main = Gst.Pipeline.set_state, threading.main_thread()

        def set_state(element, state):
            self.calls.append((state, threading.current_thread() is main))
            self.pipelines.append(element)
            if state == blocked and not self.release.is_set():
                self.entered.set()
                if on_enter is not None:
                    GLib.idle_add(on_enter)
                assert self.release.wait(20)  # Hang guard only.
            result = real(element, state)
            if state == Gst.State.NULL:
                self.closed.set()
            return result

        monkeypatch.setattr(Gst.Pipeline, "set_state", set_state)


def run_blocked_take(monkeypatch, blocked_state, on_enter=None, prepare=None, *, source=None, open_timeout_s=None):
    """Runs one take until `finished` is reported on the main loop, with `blocked_state` still held.

    Returns before the held state change is released: the caller asserts the
    report did not wait for GStreamer, then calls `finish_blocked_take`.
    """
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer introspection is unavailable")
    from gi.repository import GLib, Gst

    Gst.init(None)
    if Gst.ElementFactory.find("audiotestsrc") is None or Gst.ElementFactory.find("appsink") is None:
        pytest.skip("audiotestsrc/appsink are unavailable")
    from cassini_talk.phrase_recorder import PhraseRecorder

    recorder = PhraseRecorder(
        source or "audiotestsrc is-live=true samplesperbuffer=1600 ! audio/x-raw,rate=16000",
        open_timeout_s=open_timeout_s,
    )
    done, loop, seen = [], GLib.MainLoop(), {}
    state = getattr(Gst.State, blocked_state)
    block = BlockedStateChange(monkeypatch, GLib, Gst, state, on_enter and (lambda: on_enter(recorder, block, done)))
    recorder.connect("finished", lambda _r, ok: done.append(ok))
    report = recorder._report

    def reported():
        report()
        seen["released_at_report"] = block.release.is_set()
        loop.quit()  # The take is settled; GStreamer may still be held.
        return False

    recorder._report = reported
    if prepare:
        prepare(recorder)
    recorder.start()  # Returns at once: only the worker blocks.
    guard = GLib.timeout_add_seconds(20, loop.quit)  # Hang guard only; nothing asserts elapsed time.
    loop.run()
    GLib.source_remove(guard)
    assert seen == dict(released_at_report=False)  # Reported while GStreamer was still held.
    return Gst, recorder, block, done


def finish_blocked_take(Gst, block):
    """Lets the held state change through and waits for the background NULL teardown."""
    assert block.entered.wait(20)  # The held state change was reached (hang guard only).
    assert not block.closed.is_set()
    block.release.set()
    assert block.closed.wait(20)  # Hang guard only.
    assert all(not on_main for _state, on_main in block.calls)  # No state change on the main thread.
    assert block.pipelines[-1].get_state(0)[1] == Gst.State.NULL  # No live microphone is left.


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_microphone_that_never_answers_never_blocks_the_main_loop(monkeypatch, action):
    seen = {}

    def while_opening(recorder, block, done):
        # The main loop runs this queued callback while PLAYING is still held.
        seen["held"] = not block.release.is_set()
        getattr(recorder, action)()  # Returns at once although startup is in flight.
        seen["listening"], seen["finished"] = recorder.listening, list(done)
        return False

    Gst, recorder, block, done = run_blocked_take(monkeypatch, "PLAYING", while_opening)
    # Stop is answered on the main loop while PLAYING is still held.
    assert seen == dict(held=True, listening=False, finished=[])
    assert done == ([] if action == "cancel" else [True]) and not recorder.listening
    assert [state for state, _ in block.calls] == [Gst.State.PLAYING]
    finish_blocked_take(Gst, block)
    assert [state for state, _ in block.calls] == [Gst.State.PLAYING, Gst.State.NULL]
    assert done == ([] if action == "cancel" else [True])  # Never reported twice.
    if action == "cancel":
        assert len(recorder.samples) == 0


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_stop_and_cancel_close_the_microphone_off_the_main_thread(monkeypatch, action):
    seen = {}

    def prepare(recorder):
        def on_level(*_):
            if "acted" not in seen and len(recorder.samples) >= 4 * RATE:
                seen["acted"] = True
                getattr(recorder, action)()  # Returns at once; NULL is held on the close worker.

        recorder.connect("level", on_level)

    # A non-live source delivers a valid 4+ s take at once (or stops itself at the take bound).
    Gst, recorder, block, done = run_blocked_take(
        monkeypatch,
        "NULL",
        prepare=prepare,
        source="audiotestsrc is-live=false samplesperbuffer=8000 wave=sine volume=0.2 ! audio/x-raw,rate=16000",
    )
    assert done == ([] if action == "cancel" else [True]) and not recorder.listening
    taken = len(recorder.samples)
    if action == "stop":
        assert taken >= 4 * RATE
    finish_blocked_take(Gst, block)
    assert done == ([] if action == "cancel" else [True])  # Never reported twice.
    assert len(recorder.samples) == taken  # The sealed take is untouched by the teardown.


def test_microphone_that_never_opens_ends_as_unavailable(monkeypatch):
    """A stalled PLAYING ends on the open bound with ok=False (deviceUnavailable, not tooShort)."""
    Gst, recorder, block, done = run_blocked_take(monkeypatch, "PLAYING", open_timeout_s=0.05)
    assert done == [False] and not recorder.listening and len(recorder.samples) == 0
    finish_blocked_take(Gst, block)
    assert [state for state, _ in block.calls] == [Gst.State.PLAYING, Gst.State.NULL]
    assert done == [False]  # The late PLAYING never reports again.


def test_microphone_that_fails_to_open_reports_unavailable_not_too_short(monkeypatch):
    """PLAYING failing after a stall reports ok=False, never the tooShort ok=True with no audio."""
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer introspection is unavailable")
    from gi.repository import GLib, Gst

    Gst.init(None)
    if Gst.ElementFactory.find("audiotestsrc") is None or Gst.ElementFactory.find("appsink") is None:
        pytest.skip("audiotestsrc/appsink are unavailable")
    from cassini_talk.phrase_recorder import PhraseRecorder

    real = Gst.Pipeline.set_state

    def set_state(element, state):
        if state == Gst.State.PLAYING:
            return Gst.StateChangeReturn.FAILURE
        return real(element, state)

    monkeypatch.setattr(Gst.Pipeline, "set_state", set_state)
    recorder = PhraseRecorder("audiotestsrc is-live=true ! audio/x-raw,rate=16000")
    done, loop = [], GLib.MainLoop()
    recorder.connect("finished", lambda _r, ok: (done.append(ok), loop.quit()))
    recorder.start()
    guard = GLib.timeout_add_seconds(20, loop.quit)  # Hang guard only.
    loop.run()
    GLib.source_remove(guard)
    assert done == [False] and not recorder.listening


def test_desktop_draws_no_phone_backup_or_transfer_copy():
    """HARMONY §12 / D98: GNOME has no cloud backup, so no backup qualifier, card or switch exists."""
    phone_only = re.compile(
        r'"(backup\.[\w.]+|firstRun\.hero\.(phoneBackupQualifier|subtitlePhoneNote)|'
        r'privacy\.onDevice\.(phone|iphone|ipad)[\w.]*|settings\.transfer[\w.]*)"'
    )
    for path in (ROOT / "src/cassini_talk").glob("*.py"):
        assert not phone_only.search(path.read_text(encoding="utf-8")), path.name
