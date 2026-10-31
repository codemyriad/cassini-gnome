# SPDX-License-Identifier: GPL-3.0-or-later
"""Mute is an observed output property, never a guess from silent PCM."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from cassini_viewer.output_mute import OutputState, read_output_state


def reply(monkeypatch, sinks):
    def run(command, **options):
        # Only a bounded, read-only request; a mute check must not reroute,
        # unmute, or open a recording stream to light a privacy indicator.
        assert command == ["pactl", "--format=json", "list", "sinks"]
        assert options["timeout"] == 1 and options["check"]
        return SimpleNamespace(stdout=json.dumps(sinks))

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("muted", [True, False])
def test_uses_the_captured_monitor_even_if_a_different_output_is_default(monkeypatch, muted):
    reply(
        monkeypatch,
        [
            dict(name="speakers", monitor_source="speakers.monitor", mute=not muted),
            dict(name="headphones", monitor_source="headphones.monitor", mute=muted),
        ],
    )
    assert read_output_state("headphones.monitor") == OutputState("headphones.monitor", "headphones", muted)


@pytest.mark.parametrize("value", [None, "", "mic", "missing.monitor"])
def test_missing_or_non_monitor_source_is_unknown(monkeypatch, value):
    reply(monkeypatch, [dict(name="speakers", monitor_source="speakers.monitor", mute=True)])
    assert read_output_state(value) is None


@pytest.mark.parametrize(
    "sinks", [None, {}, [None], [dict(name="speakers", monitor_source="speakers.monitor", mute=1)]]
)
def test_bad_server_metadata_never_claims_an_output_is_unmuted(monkeypatch, sinks):
    reply(monkeypatch, sinks)
    assert read_output_state("speakers.monitor") is None


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("pactl", 1), subprocess.CalledProcessError(1, "pactl")]
)
def test_missing_tool_and_failed_or_timed_out_server_are_unknown(monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert read_output_state("speakers.monitor") is None


def test_history_records_only_transitions_and_preserves_unrelated_edits(tmp_path):
    from cassini_viewer.meeting_store import read_metadata, update_metadata
    from cassini_viewer.output_mute import MuteHistory, currently_muted, intervals
    from cassini_viewer.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    history = MuteHistory(session.directory)
    update_metadata(session.directory, recordingDetails=dict(future=42, audioHealth=dict(future="kept")))
    history.observe("system", "one.monitor", True, 0)
    first = read_metadata(session.directory)
    assert currently_muted(first)
    history.observe("system", "one.monitor", True, 100)
    assert read_metadata(session.directory) == first  # No per-tick rewrite.
    update_metadata(session.directory, title="Edited during recording", titleSource="user")
    history.observe("system", "one.monitor", None, 200)  # Unknown is never unmuted.
    history.observe("system", "one.monitor", False, 300)
    history.observe("system", "two.monitor", True, 500)
    history.observe("system-2", "two.monitor", True, 700)  # Fresh fallback span.
    history.observe(None, None, None, 900)  # Off / restored / stopped.
    result = read_metadata(session.directory)
    assert result["title"] == "Edited during recording"
    assert result["recordingDetails"]["future"] == 42
    assert result["recordingDetails"]["audioHealth"]["future"] == "kept"
    assert intervals(result) == [
        dict(trackId="system", startMs=0, endMs=300),
        dict(trackId="system", startMs=500, endMs=700),
        dict(trackId="system-2", startMs=700, endMs=900),
    ]
    assert not currently_muted(result)
    session.finish()


@pytest.mark.parametrize(
    "rows", [None, {}, [None], [dict(trackId="s", startMs=True)], [dict(trackId="s", startMs=2, endMs=1)]]
)
def test_untrusted_facts_are_not_mute_evidence(rows):
    from cassini_viewer.output_mute import intervals

    assert intervals(dict(recordingDetails=dict(audioHealth=dict(outputMuted=rows)))) == []


def test_worker_waits_for_explicit_refresh_and_stop_without_a_fixed_test_wait(tmp_path):
    import threading

    from cassini_viewer.meeting_store import read_metadata
    from cassini_viewer.output_mute import OutputMuteObserver, intervals
    from cassini_viewer.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    state = dict(track="system", monitor="sink.monitor", muted=True, at=10)
    applied = threading.Event()
    worker = OutputMuteObserver(
        session.directory,
        lambda: (state["track"], state["monitor"], state["at"]),
        lambda _muted: applied.set(),
        query=lambda monitor: OutputState(monitor, "sink", state["muted"]),
    )
    worker.start()
    try:
        assert applied.wait(3), "Muted-output observation was not persisted"
        assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10)]
        applied.clear()
        state.update(muted=False, at=100)
        worker.refresh()
        assert applied.wait(3), "Unmute observation was not persisted"
        assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10, endMs=100)]
        applied.clear()
        state.update(muted=True, at=200)
        worker.refresh()
        assert applied.wait(3), "Second muted span was not persisted"
    finally:
        worker.close(300)
        session.finish()
    assert not worker.thread.is_alive(), "Mute observer survived Stop"
    assert intervals(read_metadata(session.directory))[-1] == dict(trackId="system", startMs=200, endMs=300)


def test_failed_observation_does_not_end_capture_and_next_refresh_can_recover(tmp_path):
    import threading

    from cassini_viewer.output_mute import OutputMuteObserver
    from cassini_viewer.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    failed, applied, retry = threading.Event(), threading.Event(), threading.Event()

    def query(monitor):
        if not retry.is_set():
            failed.set()
            raise OSError("Synthetic server unavailable")
        return OutputState(monitor, "sink", True)

    observer = OutputMuteObserver(
        session.directory,
        lambda: ("system", "sink.monitor", 20),
        lambda muted: applied.set() if muted else None,
        query=query,
    )
    observer.start()
    try:
        assert failed.wait(3), "The failing server query was not attempted"
        assert not session.finished, "Mute observation failure stopped capture"
        retry.set()
        observer.refresh()
        assert applied.wait(3), "Observer did not recover after the server returned"
    finally:
        observer.close(30)
        session.finish()


def test_packaged_monitor_observer_has_its_read_only_query_tool():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "pulseaudio-utils," in (root / "debian/control").read_text()
    assert "Requires:       pulseaudio-utils" in (root / "build-aux/rpm/cassini-viewer.spec").read_text()
    assert "  libpulse " in (root / "build-aux/arch/PKGBUILD").read_text()
