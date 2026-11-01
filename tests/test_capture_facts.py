# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional end and successful writing facts preserve the meeting and capture."""

from types import SimpleNamespace

import pytest
from test_library import capture, fingerprint, write_metadata
from test_transcription import until

from cassini_talk.capture_facts import record_end, separation_result, separation_unavailable
from cassini_talk.library import LibraryScanner
from cassini_talk.library_schema import valid
from cassini_talk.meeting_store import read_metadata, update_metadata
from cassini_talk.nemotron_model import MODEL_ID, MODEL_TAG, REVISION
from cassini_talk.transcription import REFERENCE, TranscriptionQueue


def test_manual_end_is_app_owned_exact_and_preserves_metadata(tmp_path):
    session = capture(tmp_path)
    write_metadata(session.directory, notes=[dict(atMs=2, text="Kept")], futureField={"keep": True})
    before = fingerprint(session.directory)
    assert record_end(session.directory, "user", manifest=session.manifest)
    after = read_metadata(session.directory)
    assert after["recordingDetails"]["end"] == dict(reason="user", atMs=700)
    assert after["notes"] == [dict(atMs=2, text="Kept")] and after["futureField"] == {"keep": True}
    assert all(fingerprint(session.directory)[key] == value for key, value in before.items() if key != "meeting.json")
    assert not record_end(session.directory, "user", manifest=session.manifest)
    assert read_metadata(session.directory)["revision"] == after["revision"]


def test_end_handles_fractional_unknown_timing_and_actual_error(tmp_path):
    session = capture(tmp_path)
    manifest = dict(session.manifest, tracks=[dict(frames=1, sampleRate=8000, startOffsetMs=0.9)])
    record_end(session.directory, "user", manifest=manifest)
    assert read_metadata(session.directory)["recordingDetails"]["end"]["atMs"] == 1
    update_metadata(session.directory, recordingDetails=dict(end=dict(reason="user", atMs=1, futureEnd=True)))
    manifest = dict(session.manifest, tracks=[dict(frames=1, sampleRate=8000, startOffsetMs=None)], problemKind="io")
    record_end(session.directory, "user", manifest=manifest)
    assert read_metadata(session.directory)["recordingDetails"]["end"] == dict(reason="error", futureEnd=True)


def test_live_capture_does_not_claim_stop_completed(tmp_path):
    session = capture(tmp_path, completed=False)
    try:
        assert not record_end(session.directory, "user", manifest=session.manifest)
        assert not (session.directory / "meeting.json").exists()
    finally:
        session.finish()


STALE_FALLBACK = dict(
    method="vadFreeWindows",
    windowMs=15000,
    overlapMs=500,
    tracks=[dict(trackId="old", vadWords=0, fallbackWords=9, activeSeconds=20.0, used=True)],
)
FALLBACK = dict(
    method="vadFreeWindows",
    windowMs=15000,
    overlapMs=500,
    tracks=[dict(trackId="mic", vadWords=3, fallbackWords=12, activeSeconds=31.5, used=True)],
)


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize(("previous", "resumed"), [("none", False), ("running", True), ("failed", False)])
def test_successful_job_records_this_attempt_wall_time_and_skip_reason(tmp_path, previous, resumed, overlap):
    session = capture(tmp_path)
    session.manifest["tracks"][1]["kind"] = "system"
    if overlap:
        for track in session.manifest["tracks"]:
            track.update(kind="remote", sourceTrackId="same-remote", startOffsetMs=0)
    session.checkpoint()
    write_metadata(
        session.directory, transcript=dict(status=previous, reference=REFERENCE if previous != "none" else None)
    )
    update_metadata(
        session.directory,
        recordingDetails=dict(
            futureDetail=True,
            writingDown=dict(
                wallDurationMs=9000,
                resumed=False,
                speechSegments=99,
                sourceSpansSeparated=True,
                recognitionFallback=STALE_FALLBACK,
                futureWriting={"keep": True},
                speakerSeparation=dict(
                    status="completed",
                    modelId="old-model",
                    modelRevision="a" * 64,
                    speakersFound=42,
                    reused=True,
                    futureSeparation={"keep": True},
                ),
            ),
        ),
    )
    before = {p.name: p.read_bytes() for p in session.directory.iterdir() if p.name != "meeting.json"}
    done = []
    clock = iter((10.0, 14.25))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(text="A word", startMs=0, endMs=100)],
                )
            )
            record_end(session.directory, "user", manifest=session.manifest)
            update_metadata(session.directory, title="Edited during writing", notes=[dict(atMs=2, text="Keep")])
            # A real worker reports G10 decisions only when a track met the gate.
            return dict(segments=3, diagnostics=[], **({"fallback": FALLBACK} if overlap else {}))

    models = SimpleNamespace(request=lambda *_args, **_kwargs: None, snapshot=lambda: dict(downloaded=False))
    queue = TranscriptionQueue(
        lambda _: None,
        done.append,
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=models,
        clock=lambda: next(clock),
    )
    try:
        assert queue.enqueue(session.directory, resume=resumed)
        until(lambda: done)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done"
        assert metadata["title"] == "Edited during writing" and metadata["notes"] == [dict(atMs=2, text="Keep")]
        details = metadata["recordingDetails"]
        assert details["end"] == dict(reason="user", atMs=200 if overlap else 700) and details["futureDetail"] is True
        assert details["writingDown"] == dict(
            wallDurationMs=4250,
            resumed=resumed,
            speechSegments=3,
            speakerSeparation=dict(
                status="skipped",
                reason="notApplicable" if overlap else "modelsNotInstalled",
                futureSeparation={"keep": True},
            ),
            futureWriting={"keep": True},
            # This attempt's decisions replace, never merge with, an older attempt's.
            **({"sourceSpansSeparated": True, "recognitionFallback": FALLBACK} if overlap else {}),
        )
        assert valid(metadata)
        from cassini_talk.capture_details import read_details

        report = read_details(LibraryScanner(tmp_path).scan().meetings[0])
        assert ("Overlapping audio was kept as separate speakers." in report.plain_text()) == overlap
        assert all((session.directory / name).read_bytes() == value for name, value in before.items())
    finally:
        queue.close()


def test_separation_facts_count_native_voices_and_report_cache_partial_or_failure():
    group = dict(
        id="others",
        durationMs=3000,
        turns=[dict(startMs=0, endMs=1000, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)],
    )
    result = dict(model=MODEL_TAG, diagnostics=[])
    facts = separation_result(result, [group], {"others"})
    assert facts == dict(status="completed", modelId=MODEL_ID, modelRevision=REVISION, speakersFound=2, reused=True)
    assert (
        separation_result(dict(result, diagnostics=[dict(code="diarization-kept-d11")]), [group], set())["status"]
        == "partial"
    )
    assert separation_result(None, [], set()) == dict(status="failed", reason="processingFailed")
    assert separation_result(result, [dict(group, durationMs=199)], set())["reason"] == "tooShort"
    remote = [dict(id="remote", kind="remote", speaker="track-remote", frames=16000, sampleRate=16000)]
    assert separation_unavailable(None, remote, "own")["reason"] == "notApplicable"


def test_optional_details_schema_rejects_false_measurements_but_accepts_absence(tmp_path):
    session = capture(tmp_path)
    metadata = write_metadata(session.directory)
    assert valid(metadata)
    facts = dict(
        wallDurationMs=4250, resumed=False, speakerSeparation=dict(status="skipped", reason="modelsNotInstalled")
    )
    assert valid(dict(metadata, recordingDetails=dict(writingDown=facts)))
    assert not valid(dict(metadata, recordingDetails=dict(writingDown=dict(facts, wallDurationMs=True))))
    assert not valid(
        dict(metadata, recordingDetails=dict(writingDown=dict(facts, speakerSeparation=dict(status="completed"))))
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        dict(FALLBACK, method="mix"),
        dict(FALLBACK, windowMs=0),
        dict(FALLBACK, tracks=[]),
        dict(FALLBACK, tracks=[dict(FALLBACK["tracks"][0], used="yes")]),
        dict(FALLBACK, tracks=[dict(FALLBACK["tracks"][0], vadWords=-1)]),
        dict(FALLBACK, tracks="not a list"),
    ],
)
def test_malformed_fallback_reports_are_not_stored(value):
    from cassini_talk.capture_facts import recognition_fallback

    assert recognition_fallback(value) is None
    assert recognition_fallback(FALLBACK) == FALLBACK
