# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for WP5 Transcribing status line, popover model, and queue step progress (L-45, L-48)."""

import locale
from types import SimpleNamespace

import pytest
from durable import Changes, Finished
from test_library import capture, write_metadata

from cassini_talk import ux_formats as fmt
from cassini_talk.i18n import using_language
from cassini_talk.recording_session import RecordingSession
from cassini_talk.strings import plural, text
from cassini_talk.transcribing_status import (
    STEP_INDEX,
    STEPS,
    ResolvedTranscribingState,
    popover_stat_values,
    resolve_transcribing_state,
    status_line_parts,
)
from cassini_talk.transcription import TranscriptionProgress, TranscriptionQueue


def _fake_meeting(
    *,
    transcript: str = "running",
    duration_ms: int = 2_830_000,
    eta_minutes: int | None = 4,
    word_ends_ms: tuple[int, ...] = (),
):
    words = tuple(SimpleNamespace(end_ms=end_ms) for end_ms in word_ends_ms)
    return SimpleNamespace(
        transcript=transcript,
        duration_ms=duration_ms,
        eta_minutes=eta_minutes,
        words=words,
    )


def test_status_line_parts_contract_strings_and_eta_variants():
    assert status_line_parts(None) == ("Transcribing…", None)
    assert status_line_parts(0) == ("Transcribing, less than a minute left", None)
    assert status_line_parts(1) == ("Transcribing", "· about 1 min left")
    assert status_line_parts(4) == ("Transcribing", "· about 4 min left")

    with using_language("it"):
        assert status_line_parts(None) == (text("states.badge.transcribing.noEta"), None)
        assert status_line_parts(0) == (text("states.badge.transcribing.underMinute"), None)
        assert status_line_parts(4) == (text("transcribing.heading"), "· " + plural("transcribing.eta.minutes", 4))


def test_resolve_transcribing_state_and_popover_stats_match_wp5_board():
    meeting = _fake_meeting(transcript="running", duration_ms=2_830_000, eta_minutes=4)
    live = TranscriptionProgress(
        step="words",
        fraction=0.62,
        audio_done_ms=1_792_000,
        words_so_far=4812,
        eta_minutes=4,
    )
    resolved = resolve_transcribing_state(meeting, live)
    assert resolved == ResolvedTranscribingState(
        step="words",
        percent=62,
        fraction=0.62,
        eta_minutes=4,
        audio_done_ms=1_792_000,
        total_ms=2_830_000,
        words_so_far=4812,
    )
    assert popover_stat_values(resolved) == {
        "percent": "62%",
        "time_left": "About 4 min",
        "audio_done": "29:52 of 47:10",
        "words_so_far": fmt.number(4812),
    }
    prev_numeric = locale.setlocale(locale.LC_NUMERIC)
    try:
        locale.setlocale(locale.LC_NUMERIC, "en_GB.UTF-8")
    except locale.Error:
        pass
    else:
        expected_grouped = "4,812" if locale.localeconv().get("thousands_sep") == "," else fmt.number(4812)
        assert popover_stat_values(resolved)["words_so_far"] == expected_grouped
    finally:
        locale.setlocale(locale.LC_NUMERIC, prev_numeric)

    under_min = resolve_transcribing_state(
        meeting,
        TranscriptionProgress(step="saving", fraction=1.2, audio_done_ms=9_999_999, words_so_far=5000, eta_minutes=0),
    )
    assert under_min.percent == 99
    assert under_min.audio_done_ms == 2_830_000
    assert popover_stat_values(under_min)["time_left"] == "under 1 min"

    queued_fallback = resolve_transcribing_state(_fake_meeting(transcript="queued", eta_minutes=None))
    assert queued_fallback.step == "reading"
    assert queued_fallback.percent == 0
    assert popover_stat_values(queued_fallback)["time_left"] == "—"

    running_from_snapshot = resolve_transcribing_state(
        _fake_meeting(transcript="running", duration_ms=100_000, eta_minutes=2, word_ends_ms=(25_000, 50_000))
    )
    assert running_from_snapshot.step == "words"
    assert running_from_snapshot.percent == 50
    assert running_from_snapshot.audio_done_ms == 50_000
    assert running_from_snapshot.words_so_far == 2


def test_transcription_queue_emits_live_step_transitions_and_clears_on_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    write_metadata(session.directory)

    changes = Changes()
    completed = Finished()
    observed: list[TranscriptionProgress] = []

    class FakeDiarizationModels:
        def request(self, _directory, *, origin):
            _ = origin
            return {"modelPath": "/tmp/fake", "runtimePath": "/tmp/fake"}

        def measured(self, _factor):
            pass

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify({"type": "planning", "trackId": tracks[0]["id"]})
            notify({"type": "planned", "etaMinutes": 4, "segments": 2})
            words_t0 = [
                {"startMs": 0, "endMs": 250, "text": "Hello"},
                {"startMs": 260, "endMs": 340, "text": "there"},
            ]
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[0]["id"],
                    "trackOrder": 0,
                    "language": "en",
                    "etaMinutes": 3,
                    "segment": {"startSample": 0, "endSample": 1600},
                    "words": words_t0,
                }
            )
            # Mid-recognition segment cache write (speech_pipeline.py:617) and decode heartbeat (:654)
            # emit "planning" messages after "words" has already started; step must never regress.
            notify(
                {
                    "type": "planning",
                    "trackId": tracks[0]["id"],
                    "cacheEntry": {
                        "key": "a" * 64,
                        "text": "Hello there",
                        "words": words_t0,
                        "language": "en",
                    },
                }
            )
            notify({"type": "planning", "trackId": tracks[1]["id"]})
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[1]["id"],
                    "trackOrder": 1,
                    "language": "en",
                    "etaMinutes": 2,
                    "segment": {"startSample": 0, "endSample": 3200},
                    "words": [{"startMs": 500, "endMs": 680, "text": "again"}],
                }
            )
            summary = {"language": "en", "diagnostics": [], "segments": 2}
            notify({"type": "recognised", "summary": summary, "etaMinutes": 1})
            notify({"type": "diarizing", "etaMinutes": 1})
            return {
                **summary,
                "diarization": {"model": "fake", "params": {}, "groupIds": [], "diagnostics": []},
            }

    queue_holder: list[TranscriptionQueue] = []

    def on_progress(directory):
        prog = queue_holder[0].progress_for(directory)
        if prog is not None:
            observed.append(prog)

    queue = TranscriptionQueue(
        changes,
        completed,
        root=tmp_path,
        client_factory=Client,
        diarization_models=FakeDiarizationModels(),
        progress_changed=on_progress,
    )
    queue_holder.append(queue)
    try:
        assert queue.enqueue(session.directory)
        initial = queue.progress_for(session.directory)
        assert initial is None or initial.step in {k for k, _ in STEPS}
        completed.wait(1)
        assert queue.progress_for(session.directory) is None
    finally:
        queue.close()

    steps_seen = [p.step for p in observed]
    for expected in ("preparing", "words", "speakers", "saving"):
        assert expected in steps_seen, f"Missing live step {expected!r} in {steps_seen!r}"
    assert [STEP_INDEX[p.step] for p in observed] == sorted(STEP_INDEX[p.step] for p in observed), (
        f"Popover step regressed during recognition: {steps_seen!r}"
    )
    first_words = next(p for p in observed if p.step == "words" and p.words_so_far == 2)
    assert first_words.eta_minutes == 3
    assert 0 < first_words.audio_done_ms < 700
    second_words = next(p for p in observed if p.step == "words" and p.words_so_far == 3)
    assert second_words.audio_done_ms == 700
    assert [p.fraction for p in observed] == sorted(p.fraction for p in observed)
    assert [p.audio_done_ms for p in observed] == sorted(p.audio_done_ms for p in observed)


def test_multitrack_audio_done_uses_combined_work_ratio_and_resume_resets_step(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = RecordingSession(tmp_path, "Two-track meeting", clock=lambda: 0.0)
    session.add_track("mic", "Microphone", "microphone", 48000, 1)
    session.add_track("others", "System audio", "remote", 48000, 1)
    # 10,000 ms (5 x 96,000 frames @ 48 kHz -> 160,000 samples @ 16 kHz) on each track.
    for chunk_idx in range(5):
        session.append("mic", b"\x02\x00" * 96_000, chunk_idx * 2_000)
        session.append("others", b"\x02\x00" * 96_000, chunk_idx * 2_000)
    session.checkpoint()
    session.finish()
    write_metadata(session.directory)

    changes = Changes()
    completed = Finished()
    observed_runs: list[list[TranscriptionProgress]] = [[]]
    attempt = [0]

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            attempt[0] += 1
            notify({"type": "planning", "trackId": tracks[0]["id"]})
            notify({"type": "planned", "etaMinutes": 4, "segments": 2})
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[0]["id"],
                    "trackOrder": 0,
                    "language": "en",
                    "etaMinutes": 2,
                    "segment": {"startSample": 0, "endSample": 160_000},
                    "words": [{"startMs": 0, "endMs": 9_800, "text": "First"}],
                }
            )
            if attempt[0] == 1:
                raise RuntimeError("boom after first track")
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[1]["id"],
                    "trackOrder": 1,
                    "language": "en",
                    "etaMinutes": 1,
                    "segment": {"startSample": 0, "endSample": 80_000},
                    "words": [{"startMs": 1_000, "endMs": 4_500, "text": "Second"}],
                }
            )
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[1]["id"],
                    "trackOrder": 1,
                    "language": "en",
                    "etaMinutes": 0,
                    "segment": {"startSample": 80_000, "endSample": 160_000},
                    "words": [{"startMs": 5_000, "endMs": 9_900, "text": "Third"}],
                }
            )
            summary = {"language": "en", "diagnostics": [], "segments": 3}
            notify({"type": "recognised", "summary": summary, "etaMinutes": 0})
            return summary

    queue_holder: list[TranscriptionQueue] = []

    def on_progress(directory):
        prog = queue_holder[0].progress_for(directory)
        if prog is not None:
            observed_runs[-1].append(prog)

    queue = TranscriptionQueue(
        changes,
        completed,
        root=tmp_path,
        client_factory=Client,
        progress_changed=on_progress,
    )
    queue_holder.append(queue)
    try:
        assert queue.enqueue(session.directory)
        completed.wait(1)
        run1 = list(observed_runs[-1])
        t0_done = next(p for p in run1 if p.step == "words" and p.words_so_far >= 1)
        # After track 0 finishes (50% of combined 16 kHz samples), audio_done_ms must be ~half
        # of the 10,000 ms meeting rather than jumping to full length while track 1 remains.
        assert t0_done.audio_done_ms == 5_000
        assert t0_done.audio_done_ms < 10_000

        # Retry/resume starts a fresh _run, so step legitimately restarts at "preparing".
        observed_runs.append([])
        assert queue.enqueue(session.directory)
        completed.wait(2)
        run2 = list(observed_runs[-1])
        assert run2[0].step == "preparing"
        assert [STEP_INDEX[p.step] for p in run2] == sorted(STEP_INDEX[p.step] for p in run2)
        words_run2 = [p for p in run2 if p.step == "words"]
        assert [p.audio_done_ms for p in words_run2] == [0, 5_000, 7_500, 10_000]
    finally:
        queue.close()


@pytest.mark.parametrize("dark", (False, True))
def test_headless_gsk_transcribing_primitives_render_visible_ink(dark):
    gi = pytest.importorskip("gi")
    try:
        for namespace in ("Gtk", "Gdk", "Gsk"):
            gi.require_version(namespace, "4.0")
        gi.require_version("Graphene", "1.0")
    except ValueError:
        pytest.skip("GTK / GSK introspection is unavailable")
    from gi.repository import Gdk, Graphene, Gsk, Gtk

    from cassini_talk.transcribing_status import (
        snapshot_chevron,
        snapshot_progress_bar,
        snapshot_spinner,
        snapshot_step_indicator,
    )

    def render_max_alpha(draw_fn, w: int, h: int) -> int:
        snap = Gtk.Snapshot.new()
        draw_fn(snap)
        node = snap.to_node()
        assert node is not None
        texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
        gbytes, stride = downloader.download_bytes()
        buf = gbytes.get_data()
        return max(buf[y * stride + x * 4 + 3] for y in range(h) for x in range(w))

    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    try:
        assert render_max_alpha(lambda s: snapshot_spinner(s, 14, 45.0, dark=dark), 14, 14) >= 200
        for state in ("done", "active", "upcoming"):
            assert render_max_alpha(lambda s, st=state: snapshot_step_indicator(s, st, dark=dark), 16, 16) >= 200
        assert render_max_alpha(lambda s: snapshot_chevron(s, dark=dark), 7, 11) >= 180
        assert render_max_alpha(lambda s: snapshot_progress_bar(s, 120, 4, 0.62, dark=dark), 120, 4) >= 200
    finally:
        renderer.unrealize()
