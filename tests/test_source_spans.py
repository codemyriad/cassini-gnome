# SPDX-License-Identifier: GPL-3.0-or-later
"""R64 candidate IDs are literal review outputs, not a shared-contract edit."""

import json
from dataclasses import replace

import pytest

from cassini_talk.diarization import group_tracks
from cassini_talk.source_spans import separate


def track(identifier, start, frames=48000, *, speaker="track-receiver", **changes):
    return dict(
        id=identifier, speaker=speaker, kind="remote", offsetMs=start, frames=frames, sampleRate=48000, **changes
    )


@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            [track("a", 0), track("b", 1000), track("c", 2000)],
            {"a": "track-receiver", "b": "track-receiver", "c": "track-receiver"},
        ),
        (
            [track("a", 0, 96000), track("b", 1000, 96000), track("c", 2000)],
            {"a": "track-receiver", "b": "track-receiver:overlap:1", "c": "track-receiver"},
        ),
        ([track("a", 0, 48001), track("b", 1000)], {"a": "track-receiver", "b": "track-receiver:overlap:1"}),
        (
            [track("a", 0), track("b", None), track("c", None)],
            {"a": "track-receiver", "b": "track-receiver:overlap:1", "c": "track-receiver:overlap:2"},
        ),
        (
            [track("a", 0), track("b", 0), track("c", 0, speaker="track-receiver:overlap:1")],
            {"a": "track-receiver", "b": "track-receiver:overlap:2", "c": "track-receiver:overlap:1"},
        ),
    ],
)
def test_literal_conflict_lanes_are_half_open_deterministic_and_idempotent(rows, expected):
    before = json.dumps(rows)
    actual, diagnostics = separate(rows)
    assert {t["id"]: t["speaker"] for t in actual} == expected
    assert bool(diagnostics) == (len(set(expected.values())) > len({t["speaker"] for t in rows}))
    reversed_rows, _ = separate(list(reversed(rows)))
    assert {t["id"]: t["speaker"] for t in reversed_rows} == expected
    assert separate(actual) == (actual, [])
    assert json.dumps(rows) == before
    assert [t["id"] for t in actual] == [t["id"] for t in rows]


def test_long_utf8_source_and_speaker_limits_do_not_require_platform_hashing():
    base = "track-" + "é" * 100
    separated, _ = separate([track("a", 0, speaker=base), track("b", 0, speaker=base)])
    assert [t["speaker"] for t in separated] == [base, base + ":overlap:1"]
    assert max(len(t["speaker"]) for t in separated) < 256


def test_worker_grouping_also_refuses_to_sum_raw_overlapping_spans():
    rows = [track("a", 0, 96000), track("b", 1000, 96000), track("c", 2000)]
    assert group_tracks(rows) == [
        dict(id="track-receiver", tracks=["a", "c"], eligible=False, durationMs=3000),
        dict(id="track-receiver:overlap:1", tracks=["b"], eligible=False, durationMs=3000),
    ]


def test_preparer_and_real_diarizer_input_keep_overlapping_tones_separate(tmp_path, caplog):
    np = pytest.importorskip("numpy")
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.transcription import prepare_tracks

    session = RecordingSession(tmp_path)
    for identifier, offset, hz in (("a", 0, 440), ("b", 1000, 880), ("c", 2000, 440)):
        session.add_track(identifier, "Guest", "remote", 48000, 1, source_track_id="receiver")
        signal = (np.sin(np.arange(96000) * (2 * np.pi * hz / 48000)) * 8192).astype("<i2")
        session.append(identifier, signal[:48000].tobytes(), offset)
        session.append(identifier, signal[48000:].tobytes(), offset + 1000)
    session.finish()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    before = {t.path: t.path.read_bytes() for t in meeting.tracks}
    jobs, speakers = prepare_tracks(meeting)
    assert [t["speaker"] for t in jobs] == ["track-receiver", "track-receiver:overlap:1", "track-receiver"]
    assert set(speakers) == {"track-receiver", "track-receiver:overlap:1"}
    assert len({s["colourIndex"] for s in speakers.values()}) == 2
    assert "source-spans-separated" in caplog.text
    by_id = {job["id"]: job for job in jobs}
    for group in group_tracks(jobs):
        with rejoin([by_id[i] for i in group["tracks"]], scratch=tmp_path / "scratch") as (stream, frames, _sha):
            samples = np.frombuffer(stream.read(), dtype="<f4")
        # With both tones incorrectly summed this bound exceeds .4. Each
        # actual model input must contain only its own .25-amplitude source.
        assert np.max(np.abs(samples)) < 0.26
        assert frames == group["durationMs"] * 16
        probe = samples[17600:25600]
        own_hz, other_hz = (440, 880) if group["id"] == "track-receiver" else (880, 440)

        def amplitude(hz, probe=probe):
            carrier = np.exp(2j * np.pi * hz * np.arange(len(probe)) / 16000)
            return 2 * abs(np.vdot(carrier, probe)) / len(probe)

        assert amplitude(own_hz) > 0.20 and amplitude(other_hz) < 0.001
    assert all(path.read_bytes() == data for path, data in before.items())
    # Unknown placement may be displayed at zero, but never proves disjointness.
    unknown = replace(meeting, tracks=tuple(replace(t, start_offset_ms=None) for t in meeting.tracks))
    unknown_jobs, _ = prepare_tracks(unknown)
    assert len({t["speaker"] for t in unknown_jobs}) == 3


def test_conflict_ids_do_not_look_like_d24_subvoices():
    from cassini_talk.worker_voices import group_words

    words = [
        dict(speaker="track-receiver", startMs=0, endMs=1000),
        dict(speaker="track-receiver:overlap:1", startMs=0, endMs=1000),
    ]
    # The conflicting source is not a diarized child of the base source.
    selected, labels = group_words(
        dict(id="track-receiver:overlap:1"),
        words,
        [],
        {},
        all_words=True,
        excluded_groups=("track-receiver",),
    )
    assert selected == [words[1]] and labels == {}


def test_clock_mapped_fractional_end_is_not_floored_before_overlap_check(tmp_path):
    import wave

    from cassini_talk.source_spans import bounds

    path = tmp_path / "track-mapped.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", ""))
        audio.writeframes(bytes(96000))
    mapped = track(
        "mapped",
        0,
        path=str(path),
        channels=1,
        clockAnchors=[[0, 0], [48000, 1000020833]],
    )
    assert bounds(mapped)[1] > 1000
    separated, _ = separate([mapped, track("following", 1000)])
    assert separated[1]["speaker"] == "track-receiver:overlap:1"
