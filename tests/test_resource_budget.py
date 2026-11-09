# SPDX-License-Identifier: GPL-3.0-or-later
"""G14/R75: temp disk for the voice step and diarization stays one group's size.

The fixtures are sparse 16 kHz WAVs (only short speech bursts are allocated), so
30 minutes of 14 sources cost a few MB; the bound does not depend on length. The speaker extractor and the diarizer are
faked; rejoin, grouping and window selection are the real code.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import types
from hashlib import sha256
from pathlib import Path

import pytest

pytest.importorskip("numpy")  # Core/Whistle-light profiles need no PCM worker dependencies.

from test_voice_application import application_class  # noqa: E402, F401

from cassini_talk import diarization_audio, scratch_budget, worker_diarization, worker_voices  # noqa: E402

RATE = 16000
MEETING_S = 1800
SPANS = [(0, 500), (600, 1200), (1300, MEETING_S)]  # Three spans with gaps.
BURST_S = 60


def speech_at(index):
    """Each source speaks once, for one minute, in one of the meeting's thirds and inside a span."""
    return (150, 700, 1400)[index % 3] + 61 * (index // 3)


@pytest.fixture(autouse=True)
def release(tmp_path):
    """Later suite tests need their own free space on a small test tmpfs: drop these WAVs."""
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


def usage(free, total):
    return lambda _path: types.SimpleNamespace(f_bavail=free, f_frsize=1, f_blocks=total)


ROOMY = usage(1 << 40, 1 << 41)


def write_wav(path, seconds, bursts):
    """Sparse PCM16: zeros everywhere except constant-valued bursts (local second, value)."""
    frames = seconds * RATE
    with path.open("wb") as stream:
        stream.write(
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                *(b"RIFF", frames * 2 + 36, b"WAVE", b"fmt ", 16, 1, 1, RATE, RATE * 2, 2, 16, b"data", frames * 2),
            )
        )
        for at, value in bursts:
            stream.seek(44 + at * RATE * 2)
            stream.write(struct.pack("<h", value) * (BURST_S * RATE))
        stream.truncate(44 + frames * 2)
    return frames


def meeting(root, remotes, spans):
    """Me, system and `remotes` sources; the first `spans` remotes have three spans with gaps."""
    sources = [("me", "microphone", 25), ("room", "system", 50)]
    sources += [(f"r{k}", "remote", 100 * (k + 1)) for k in range(1, remotes + 1)]
    tracks, items = [], []
    for index, (source, kind, value) in enumerate(sources):
        at = speech_at(index)
        extents = SPANS if index - 2 < spans and kind == "remote" else [(0, MEETING_S)]
        for part, (start, end) in enumerate(extents):
            bursts = [(at - start, value)] if start <= at < end else []
            path = root / f"track-{source}-{part}.wav"
            frames = write_wav(path, end - start, bursts)
            tracks.append(
                dict(id=f"{source}-{part}", sourceTrackId=source, kind=kind, path=str(path), frames=frames)
                | dict(sampleRate=RATE, offsetMs=start * 1000)
            )
        speaker = {"microphone": "me", "system": "others"}.get(kind, "track-" + source)
        items += [
            dict(speaker=speaker, text="word", startMs=(at + s) * 1000, endMs=(at + s + 1) * 1000)
            for s in range(BURST_S)
        ]
    items.sort(key=lambda item: item["startMs"])
    (root / "transcript.json").write_text(json.dumps({"format": "cassini.words.v1", "items": items}))
    return tracks


class Meter:
    """Wraps rejoin's TemporaryFile and keeps the peak of the summed apparent size of open files.

    rejoin writes zero blocks explicitly, so on a real volume a group file's allocated size is its
    apparent size wherever a source was present. Here all-zero writes become holes instead, so that
    13 x 460.8 MB on unchanged code fits the test's tmpfs; that makes st_blocks meaningless, so the
    peak is measured in st_size. Reads see the same bytes either way.
    """

    def __init__(self, monkeypatch):
        self.open, self.created, self.peak, self.peak_blocks = set(), 0, 0, 0
        self.zero_block = b""
        monkeypatch.setattr(diarization_audio, "tempfile", types.SimpleNamespace(TemporaryFile=self.temporary))

    def temporary(self, **options):
        self.created += 1
        return Holes(tempfile.TemporaryFile(**options), self)

    def measure(self):
        sizes = [os.fstat(f.fileno()) for f in self.open]
        self.peak = max(self.peak, sum(s.st_size for s in sizes))
        self.peak_blocks = max(self.peak_blocks, sum(s.st_blocks * 512 for s in sizes))


class Holes:
    def __init__(self, stream, meter):
        self.stream, self.meter = stream, meter
        meter.open.add(self)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, data):
        end = self.stream.tell() + len(data)
        # Reuse one exact zero block; byte equality avoids counting every byte.
        zero = self.meter.zero_block
        if len(zero) != len(data):
            self.meter.zero_block = zero = bytes(len(data))
        if data != zero:
            self.stream.write(data)
        else:
            self.stream.seek(end)
            if end > os.fstat(self.stream.fileno()).st_size:
                self.stream.truncate(end)
        self.meter.measure()
        return len(data)

    def close(self):
        self.meter.open.discard(self)
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def fake_extractor(monkeypatch, *, metadata=False):
    """Prints are a digest of the chunks; `seen` keeps each chunk's distinct PCM16 values, in embed order."""
    import numpy as np

    class Seen(list):
        by_digest = {}  # inputSHA256 -> the same values, for prints sent in another order.

    seen = Seen()

    class Extractor:
        def __init__(self, *_args):
            pass

        def embed(self, chunks):
            seen.append([sorted({round(v * 32768) for v in np.unique(chunk).tolist()}) for chunk in chunks])
            joined = b"".join(chunk.tobytes() for chunk in chunks)
            if not joined:
                return None
            digest = sha256(joined).hexdigest()
            seen.by_digest[digest] = seen[-1]
            return dict(embedding=[len(chunks)], seconds=len(joined) / 4 / RATE, inputSHA256=digest)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "cassini_talk.campplus", types.SimpleNamespace(SpeakerExtractor=Extractor))
    if not metadata:
        monkeypatch.setattr(worker_voices, "read_metadata", lambda _directory: {})
    return seen


@pytest.fixture
def extractor(monkeypatch):
    return fake_extractor(monkeypatch)


def voices(root, tracks, *, merges=(), statvfs=ROOMY, complete=True):
    events = []
    options = dict(directory=str(root), modelPath="m", runtimePath="r", scratch=str(root / "scratch"))
    options["speakerMerges"] = [{"from": a, "into": b} for a, b in merges]
    if complete:
        notify = lambda kind, **value: events.append(dict(type=kind, **value))  # noqa: E731
        worker_voices.run(tracks, options, notify, statvfs=statvfs)
        assert events[-1]["type"] == "voices-complete"
        return events
    # Through the request entry point, as the child runs it: errors become voices-error.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scratch_budget, "volume_usage", statvfs)
        worker_voices.request(
            dict(tracks=tracks, options=options), lambda kind, **v: events.append(dict(type=kind, **v))
        )
    return events


def voice_step(root, remotes, spans):
    """One voice step on a fresh 30-minute meeting; the child entry point of the test below."""
    monkeypatch = pytest.MonkeyPatch()
    seen = fake_extractor(monkeypatch)
    tracks = meeting(Path(root), remotes, spans)
    meter = Meter(monkeypatch)
    events = voices(Path(root), tracks, merges=[("track-r2", "track-r1")] if remotes > 1 else ())
    chunks = {e["speaker"]: seen.by_digest[e["inputSHA256"]] for e in events if e["type"] == "voice-print"}
    return dict(entries=len(tracks), peak=meter.peak, open=len(meter.open), chunks=chunks)


CHILD = """
import json, sys
sys.path[:0] = sys.argv[1:3]
import test_resource_budget
print(json.dumps(test_resource_budget.voice_step(sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))))
"""


def voice_step_in_child(root, remotes, spans):
    """(result, peak RSS in bytes) of voice_step() in a fresh process."""
    root.mkdir()
    paths = [str(Path(__file__).parents[1] / "src"), str(Path(__file__).parent)]
    command = [sys.executable, "-c", CHILD, *paths, str(root), str(remotes), str(spans)]
    child = subprocess.Popen(command, stdout=subprocess.PIPE)
    output = child.stdout.read()
    _pid, status, usage = os.wait4(child.pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return json.loads(output), usage.ru_maxrss * 1024


def test_voice_temp_disk_and_memory_stay_one_group_whatever_the_number_of_sources_and_spans(tmp_path):
    """3 track entries (Me, system, one remote) against 34 (Me, system, 12 remotes, 20 ended spans), 30 min each.

    Before G14 the voice step kept every group's rejoin file open: 2 and then 13 groups' worth.
    Memory: the 34-entry run may add at most 24 MiB to the 3-entry run's peak. Holding every
    speaker's 45 s of audio at once would add 12 x 2.88 MB.
    """
    few, few_rss = voice_step_in_child(tmp_path / "few", 1, 0)
    many, many_rss = voice_step_in_child(tmp_path / "many", 12, 10)
    assert (few["entries"], many["entries"]) == (3, 34)
    group = MEETING_S * RATE * 4  # 115.2 MB: one 30-minute group (460.8 MB at 2 h).
    assert few["peak"] == many["peak"] == group, (few["peak"], many["peak"])
    assert few["open"] == many["open"] == 0
    assert many_rss - few_rss < 24 << 20, (few_rss, many_rss)
    # Every source but Me gives a print from its own speech (merged r2 is read into r1), never zeros or a mix.
    assert set(few["chunks"]) == {"others", "track-r1"}
    assert set(many["chunks"]) == {"others"} | {f"track-r{k}" for k in range(1, 13)} - {"track-r2"}
    for result in (few, many):
        for speaker, chunks in result["chunks"].items():
            if speaker == "others":
                expected = [[50]]
            elif speaker == "track-r1" and result is many:
                expected = [[200], [300]]
            else:
                expected = [[100 * (int(speaker.removeprefix("track-r")) + 1)]]
            assert all(chunk in expected for chunk in chunks), (speaker, chunks)
            assert all(any(chunk == e for chunk in chunks) for e in expected), (speaker, chunks)


def test_low_disk_fails_the_voice_step_before_any_temp_file(tmp_path, monkeypatch, extractor):
    tracks = meeting(tmp_path, 2, 1)
    meter = Meter(monkeypatch)
    events = voices(tmp_path, tracks, statvfs=usage(1 << 30, 100 << 30), complete=False)
    assert meter.created == 0 and not extractor
    assert [e["type"] for e in events] == ["voice-progress", "voices-error"]
    needed = (MEETING_S * 1000 + 1000) * 64
    assert events[0]["diagnostic"] == dict(
        code="voices-skipped-low-disk", group="others", freeBytes=1 << 30, neededBytes=needed, reserveBytes=1 << 30
    )
    assert "Not enough disk space" in events[1]["message"]


@pytest.mark.parametrize("low_at", [2, 4])
def test_space_lost_in_either_pass_fails_the_whole_voice_step(tmp_path, monkeypatch, extractor, low_at):
    """Rejoins: others, r1, r2, then others, r1, r2 again. No partial print, even for r1 merged with r2."""
    tracks = meeting(tmp_path, 2, 0)
    meter = Meter(monkeypatch)
    calls = []

    def statvfs(path):
        calls.append(path)
        return (usage(0, 100 << 30) if len(calls) == low_at else ROOMY)(path)

    events = voices(tmp_path, tracks, merges=[("track-r2", "track-r1")], statvfs=statvfs, complete=False)
    assert events[-1]["type"] == "voices-error" and not meter.open
    assert not [e for e in events if e["type"] == "voice-print"]
    assert meter.created == low_at - 1


def test_audio_that_changes_between_the_passes_fails_the_voice_step(tmp_path, monkeypatch, extractor):
    tracks = meeting(tmp_path, 1, 0)
    room = next(Path(t["path"]) for t in tracks if t["kind"] == "system")
    calls = []

    def statvfs(path):
        calls.append(path)
        if len(calls) == 3:  # Pass 2 is about to rebuild the room's group.
            with room.open("r+b") as stream:
                stream.seek(44 + 10 * RATE * 2)
                stream.write(struct.pack("<h", 7) * RATE)
        return ROOMY(path)

    events = voices(tmp_path, tracks, statvfs=statvfs, complete=False)
    assert len(calls) == 3 and not extractor
    assert [e["type"] for e in events if e["type"] != "voice-progress"] == ["voices-error"]
    assert events[-1]["message"] == "Voice audio changed during extraction"


NEEDED = 61000 * 64  # One minute of group, plus one second.


def budget(tmp_path, free, recordings=(), statvfs=None):
    return scratch_budget.shortfall(
        60000, scratch=tmp_path / "scratch", statvfs=statvfs or usage(free, 500 << 30), recordings=recordings
    )


def test_without_a_live_recording_the_budget_keeps_only_the_1_gib_floor(tmp_path):
    """500 GiB volume: the recorder's 2 % would be 10 GiB, and must not apply."""
    floor = 1 << 30
    assert budget(tmp_path, NEEDED + floor) is None
    assert budget(tmp_path, NEEDED + floor - 1) == dict(
        freeBytes=NEEDED + floor - 1, neededBytes=NEEDED, reserveBytes=floor
    )
    # A recording that already ended (folder gone) changes nothing.
    assert budget(tmp_path, NEEDED + floor, [str(tmp_path / "gone")]) is None


def test_a_live_recording_on_the_same_volume_keeps_the_recorder_reserve(tmp_path):
    from cassini_talk.recording_details import disk_reserve

    reserve = disk_reserve(500 << 30)
    assert reserve == 10 << 30  # 2 % of 500 GiB beats 1 GiB.
    live = tmp_path / "Recording-live"
    live.mkdir()
    assert budget(tmp_path, NEEDED + reserve, [str(live)]) is None
    assert budget(tmp_path, NEEDED + reserve - 1, [str(live)]) == dict(
        freeBytes=NEEDED + reserve - 1, neededBytes=NEEDED, reserveBytes=reserve
    )


def test_a_live_recording_on_another_volume_keeps_only_the_floor(tmp_path):
    other = Path("/proc")  # Another device on every Linux system.
    assert os.stat(other).st_dev != os.stat(tmp_path).st_dev

    def statvfs(path):
        return usage(NEEDED + (1 << 30), (2 << 40) if Path(path) == other else 500 << 30)(path)

    assert budget(tmp_path, None, [str(other)], statvfs=statvfs) is None


def test_low_disk_diarization_keeps_d11_without_a_temp_file(tmp_path, monkeypatch):
    tracks = [t for t in meeting(tmp_path, 1, 0) if t["kind"] == "system"]
    meter = Meter(monkeypatch)
    monkeypatch.setitem(sys.modules, "cassini_talk.nemotron", None)  # Any model use would fail the import.
    low = usage(1 << 30, 10 << 30)
    result = worker_diarization.run(tracks, dict(scratch=tmp_path), lambda *_a, **_k: None, statvfs=low)
    assert meter.created == 0 and result["groupIds"] == []
    assert result["diagnostics"] == [
        dict(
            code="diarization-kept-d11",
            group="others",
            reason="lowDisk",
            message="Not enough disk space",
            freeBytes=1 << 30,
            neededBytes=(MEETING_S * 1000 + 1000) * 64,
            reserveBytes=1 << 30,
        )
    ]


@pytest.mark.parametrize("recording", [False, True])
def test_low_disk_diarization_still_writes_the_transcript(tmp_path, monkeypatch, caplog, recording):
    """The queue passes the recordings live at job start; one on this volume raises the reserve."""
    from test_diarization_queue import Models

    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.strings import text
    from cassini_talk.transcription import TranscriptionQueue

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))  # The default scratch folder.
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    meter = Meter(monkeypatch)
    low = usage(0, 100 << 30)
    live = tmp_path / "Recording-live"
    live.mkdir()
    words = [dict(text="One", startMs=10, endMs=800), dict(text="Two", startMs=1010, endMs=1800)]

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(
                dict(type="progress", trackId=tracks[0]["id"], trackOrder=0, language="en", etaMinutes=1, words=words)
            )
            summary = dict(language="en", diagnostics=[])
            return worker_diarization.complete(
                summary, tracks, self.diarization, lambda kind, **value: notify(dict(type=kind, **value)), statvfs=low
            )

    done = threading.Event()
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _directory: done.set(),
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
        recordings=lambda: [live] if recording else [],
    )
    try:
        with caplog.at_level("WARNING"):
            assert queue.enqueue(session.directory)
            assert done.wait(30)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [(w["text"], w["speaker"]) for w in body["items"]] == [("One", "others"), ("Two", "others")]
        separation = metadata["recordingDetails"]["writingDown"]["speakerSeparation"]
        assert separation == dict(status="skipped", reason="lowDisk")  # D56, R75 Q2.
        (meeting,) = LibraryScanner(tmp_path).scan().meetings
        shown = dict(read_details(meeting).writing)[text("details.writing.separation")]
        assert shown == text("details.separation.lowDisk")
        assert meter.created == 0 and not (session.directory / "speaker-turns.json").exists()
        assert (tmp_path / "cache/cassini/diarization").is_dir()  # Checked there, not in the real ~/.cache.
        reserve = (2 << 30) if recording else 1 << 30  # 2 % of 100 GiB, or the floor.
        assert any(
            "diarization-kept-d11" in r.message and f"'reserveBytes': {reserve}" in r.message for r in caplog.records
        )
    finally:
        queue.close()


def test_a_low_disk_skip_beside_a_diarized_group_is_partial_for_low_disk():
    """One group separated, one skipped for space: partial, and the reason says why."""
    from cassini_talk.capture_facts import separation_result
    from cassini_talk.nemotron_model import MODEL_TAG

    group = dict(id="others", durationMs=3000, turns=[dict(startMs=0, endMs=1000, speaker=0)])
    skipped = dict(code="diarization-kept-d11", group="tab", reason="lowDisk", freeBytes=1)
    failed = dict(code="diarization-kept-d11", group="media", message="Native error")
    result = dict(model=MODEL_TAG, diagnostics=[skipped])
    assert separation_result(result, [group], set())["status"] == "partial"
    assert separation_result(result, [group], set())["reason"] == "lowDisk"
    assert (
        separation_result(dict(result, diagnostics=[skipped, failed]), [group], set())["reason"] == "processingFailed"
    )
    assert separation_result(result, [], set()) == dict(status="skipped", reason="lowDisk")
    # A group too short to separate does not hide the low-disk skip of the others.
    assert separation_result(result, [dict(group, durationMs=150)], set()) == dict(status="skipped", reason="lowDisk")
    assert separation_result(dict(result, diagnostics=[]), [dict(group, durationMs=150)], set())["reason"] == "tooShort"
    assert separation_result(dict(result, diagnostics=[skipped, failed]), [], set()) == dict(
        status="failed", reason="processingFailed"
    )


def test_voice_diagnostics_reach_the_app_log(caplog):
    import io

    from cassini_talk.whistle_client import WhistleClient

    client = WhistleClient.__new__(WhistleClient)
    client.process = types.SimpleNamespace(stdin=io.BytesIO())
    replies = iter(
        [
            dict(type="voice-progress", diagnostic=dict(code="voices-skipped-low-disk", group="others", freeBytes=1)),
            dict(type="voice-progress"),
            dict(type="voices-complete"),
        ]
    )
    client._receive = lambda: next(replies)
    with caplog.at_level("WARNING"):
        assert client.voiceprints([], {}) == {}
    assert [r.message for r in caplog.records] == [
        "Voice diagnostic: {'code': 'voices-skipped-low-disk', 'group': 'others', 'freeBytes': 1}"
    ]


def test_application_lists_the_recordings_running_now(application_class, tmp_path):  # noqa: F811
    native = dict(
        busy=types.SimpleNamespace(
            pipeline=object(), session=types.SimpleNamespace(directory=tmp_path / "Recording-a")
        ),
        starting=types.SimpleNamespace(pipeline=None, starting=True, session=None),
        ended=types.SimpleNamespace(pipeline=None, session=types.SimpleNamespace(directory=tmp_path / "Recording-old")),
    )
    statuses = [
        dict(recording=True, path="/library/Recording-b"),
        dict(stopping=True, path="/library/Recording-c"),  # Still writing its PCM.
        dict(recording=False, path="/library/Recording-d"),
        dict(recording=True),
    ]
    app = types.SimpleNamespace(native_captures=native, _browser_snapshot=dict(statuses=statuses))
    assert application_class.live_recording_directories(app) == [
        str(tmp_path / "Recording-a"),
        "/library/Recording-b",
        "/library/Recording-c",
    ]


def test_the_real_budget_reads_the_volume(monkeypatch, tmp_path):
    monkeypatch.undo()  # Drop conftest's ample free space.
    assert scratch_budget.volume_usage(tmp_path).f_blocks == os.statvfs(tmp_path).f_blocks


def worker_client(monkeypatch, words):
    """A client whose voice step is the real worker, its replies read by the real client parser."""
    from cassini_talk.whistle_client import WhistleClient

    fake_extractor(monkeypatch, metadata=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def cancel(self):
            pass

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(
                dict(type="progress", trackId=tracks[0]["id"], trackOrder=0, language="en", etaMinutes=1, words=words)
            )
            return dict(diagnostics=[])

        def voiceprints(self, tracks, options, heartbeat):
            events = []
            message = json.loads(json.dumps(dict(tracks=tracks, options=options)))
            worker_voices.request(message, lambda kind, **value: events.append(dict(type=kind, **value)))
            replies = WhistleClient.__new__(WhistleClient)
            replies.process = types.SimpleNamespace(stdin=io.BytesIO())
            replies._receive = iter(events).__next__
            return replies.voiceprints(tracks, options, heartbeat)

    return Client


class VoiceModels:
    def request(self, directory, *, origin):
        return dict(modelPath="model", runtimePath="runtime", origin=origin)

    def measured(self, _factor):
        pass

    def snapshot(self):
        return dict(ready=True)


@pytest.mark.parametrize("path", ["commit_job", "recompute"])
def test_a_low_disk_voice_step_keeps_matches_and_rejections(tmp_path, monkeypatch, caplog, path):
    """Writing down (commit_job) and a speaker edit (recompute) commit nothing without disk space."""
    from test_voice_store import embedding

    from cassini_talk import voice_models
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.transcription import TranscriptionQueue
    from cassini_talk.voice_store import VoiceStore

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("sound", "The others", "system", 16000, 1)
    session.append("sound", b"\x01\x00" * 80000, 0)
    session.append("sound", b"\x01\x00" * 80000, 5000)
    session.finish()
    directory = session.directory
    words = [dict(text="Hello", startMs=0, endMs=9000)]
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    store.commit_job(directory, {"others": dict(embedding=embedding(), seconds=8)}, 0)
    store.remember(directory, "others", "Bob")
    # Writing down: Bob rejected, Alice confirmed. Edit: Bob rejected, nothing confirmed.
    store.remember(directory, "others", "Alice" if path == "commit_job" else "")
    before = store.meeting(directory, raw=True), store.snapshot()
    Client = worker_client(monkeypatch, words)
    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    monkeypatch.setattr(scratch_budget, "volume_usage", usage(0, 100 << 30))
    done, failed = threading.Event(), []
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _directory: done.set(),
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=VoiceModels(),
        voice_models=VoiceModels(),
        voice_store=store,
    )
    try:
        caplog.set_level("WARNING")
        if path == "commit_job":
            assert queue.enqueue(directory)
        else:
            (directory / "transcript.json").write_text(
                json.dumps(dict(format="cassini.words.v1", items=[dict(w, speaker="others") for w in words]))
            )
            assert queue.recompute_voices(directory, {"others"}, failed.append)
        assert done.wait(30)
        if path == "commit_job":
            assert read_metadata(directory)["transcript"]["status"] == "done"
        else:
            assert len(failed) == 1 and "Not enough disk space" in str(failed[0])
        assert any("voices-skipped-low-disk" in r.message for r in caplog.records)
        assert (store.meeting(directory, raw=True), store.snapshot()) == before
        item = before[0]["speakers"]["others"]
        assert len(item["rejected"]) == 1
        assert item["match"]["state"] == ("confirmed" if path == "commit_job" else "rejected")
    finally:
        queue.close()


def test_each_print_frees_its_window_audio_before_the_next_speaker_is_read(tmp_path, monkeypatch, extractor):
    """Two diarized voices in one group: at most one speaker's windows are held at a time."""
    from cassini_talk.nemotron_model import MODEL_TAG

    path = tmp_path / "room.wav"
    frames = write_wav(path, 300, [])
    with path.open("r+b") as stream:  # Two voices, one minute each.
        for at, value in ((10, 50), (100, 70)):
            stream.seek(44 + at * RATE * 2)
            stream.write(struct.pack("<h", value) * (BURST_S * RATE))
    tracks = [dict(id="room", kind="system", path=str(path), frames=frames, sampleRate=RATE, offsetMs=0)]
    turns = [dict(speaker=0, startMs=10000, endMs=70000), dict(speaker=1, startMs=100000, endMs=160000)]
    group = dict(id="others", durationMs=300000, inputSHA256="0" * 64, turns=turns)
    cache = dict(format=worker_diarization.FORMAT, model=MODEL_TAG, params=worker_diarization.PARAMS, groups=[group])
    (tmp_path / "speaker-turns.json").write_text(json.dumps(cache))
    items = [
        dict(speaker=f"others~{n}", text="word", startMs=(at + s) * 1000, endMs=(at + s + 1) * 1000)
        for n, at in ((1, 10), (2, 100))
        for s in range(BURST_S)
    ]
    (tmp_path / "transcript.json").write_text(json.dumps({"format": "cassini.words.v1", "items": items}))
    held, peak = [0], [0]
    read = worker_voices.read_window

    def counted(stream, start, end):
        held[0] += 1
        peak[0] = max(peak[0], held[0])
        return read(stream, start, end)

    monkeypatch.setattr(worker_voices, "read_window", counted)
    model = sys.modules["cassini_talk.campplus"].SpeakerExtractor
    embed = model.embed

    def released(self, chunks):
        held[0] -= len(chunks)
        return embed(self, chunks)

    monkeypatch.setattr(model, "embed", released)
    events = voices(tmp_path, tracks)
    prints = [e for e in events if e["type"] == "voice-print"]
    assert [p["speaker"] for p in prints] == ["others~1", "others~2"]
    assert extractor == [[[50]] * len(prints[0]["windows"]), [[70]] * len(prints[1]["windows"])]
    assert peak[0] == max(len(p["windows"]) for p in prints) and held[0] == 0


def test_a_speaker_merged_across_groups_holds_back_no_one_elses_audio(tmp_path, monkeypatch, extractor):
    """The first speaker (others, merged with r3) waits for r3's group; r1 and r2 are embedded meanwhile."""
    tracks = meeting(tmp_path, 3, 0)
    held, peak = [0], [0]
    read = worker_voices.read_window

    def counted(stream, start, end):
        held[0] += 1
        peak[0] = max(peak[0], held[0])
        return read(stream, start, end)

    monkeypatch.setattr(worker_voices, "read_window", counted)
    model = sys.modules["cassini_talk.campplus"].SpeakerExtractor
    embed = model.embed

    def released(self, chunks):
        held[0] -= len(chunks)
        return embed(self, chunks)

    monkeypatch.setattr(model, "embed", released)
    events = voices(tmp_path, tracks, merges=[("track-r3", "others")])
    sent = [(e["type"], e.get("speaker")) for e in events if e["type"] != "voice-progress" or "speaker" in e]
    # Prints still go out in plan order, each followed by its progress notice.
    assert sent == [("voice-print", s) for s in ("others", "track-r1", "track-r2")] + [("voices-complete", None)]
    kinds = [e["type"] for e in events]
    assert all(kinds[i + 1] == "voice-progress" for i, kind in enumerate(kinds) if kind == "voice-print")
    prints = {e["speaker"]: e for e in events if e["type"] == "voice-print"}
    assert {v for chunk in extractor.by_digest[prints["others"]["inputSHA256"]] for v in chunk} == {50, 400}
    assert held[0] == 0
    others = len(prints["others"]["windows"])
    assert peak[0] <= others + max(len(prints[s]["windows"]) for s in ("track-r1", "track-r2")), peak


def test_a_recording_started_while_writing_down_counts_for_the_voice_pass(tmp_path, monkeypatch, caplog):
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.transcription import TranscriptionQueue
    from cassini_talk.voice_store import VoiceStore

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("sound", "The others", "system", 16000, 1)
    session.append("sound", b"\x01\x00" * 80000, 0)
    session.finish()
    live = tmp_path / "Recording-live"
    live.mkdir()
    started = threading.Event()
    store = VoiceStore(tmp_path / "private")
    store.refresh()

    class Client(worker_client(monkeypatch, [dict(text="Hello", startMs=0, endMs=4000)])):
        def process_tracks(self, tracks, notify):
            started.set()  # A recording on this volume starts while writing down.
            return super().process_tracks(tracks, notify)

    monkeypatch.setattr(scratch_budget, "volume_usage", usage(0, 100 << 30))
    done = threading.Event()
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _directory: done.set(),
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=VoiceModels(),
        voice_models=VoiceModels(),
        voice_store=store,
        recordings=lambda: [live] if started.is_set() else [],
    )
    try:
        caplog.set_level("WARNING")
        assert queue.enqueue(session.directory)
        assert done.wait(30)
        (diagnostic,) = [r.message for r in caplog.records if "voices-skipped-low-disk" in r.message]
        assert f"'reserveBytes': {2 << 30}" in diagnostic  # 2 % of 100 GiB, not the 1 GiB floor.
    finally:
        queue.close()
