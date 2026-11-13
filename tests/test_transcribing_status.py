# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for WP5 Transcribing status line, popover model, and queue step progress (L-45, L-48)."""

import locale
from types import SimpleNamespace

import pytest
from durable import Changes, Finished
from test_library import capture, write_metadata

from cassini_talk import ux_formats as fmt
from cassini_talk.i18n import using_language
from cassini_talk.strings import plural, text
from cassini_talk.transcribing_status import (
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
        assert popover_stat_values(resolved)["words_so_far"] == "4,812"
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
            notify({"type": "planned", "etaMinutes": 4, "segments": 1})
            notify(
                {
                    "type": "progress",
                    "trackId": tracks[0]["id"],
                    "trackOrder": 0,
                    "language": "en",
                    "etaMinutes": 3,
                    "segment": {"startSample": 0, "endSample": 8000},
                    "words": [
                        {"startMs": 0, "endMs": 250, "text": "Hello"},
                        {"startMs": 260, "endMs": 500, "text": "there"},
                    ],
                }
            )
            summary = {"language": "en", "diagnostics": [], "segments": 1}
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
    words_prog = next(p for p in observed if p.step == "words" and p.words_so_far >= 2)
    assert words_prog.eta_minutes == 3
    assert words_prog.audio_done_ms >= 500
    assert [p.fraction for p in observed] == sorted(p.fraction for p in observed)


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
