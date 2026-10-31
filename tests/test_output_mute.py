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
