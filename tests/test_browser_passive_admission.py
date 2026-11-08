# SPDX-License-Identifier: GPL-3.0-or-later
"""D80.2: source observation alone must not cross the automatic Start boundary."""

import json
from pathlib import Path

import pytest
from passive_browser_proof import replay

FIXTURE = Path(__file__).parent / "fixtures/browser-passive/companion-0.2.25.json"


def packets():
    facts = json.loads(FIXTURE.read_text())
    assert facts["extensionVersion"] == "0.2.25"
    assert facts["gumCalls"] == 0
    assert facts["discovery"]["sourceCount"] == 1
    assert facts["discovery"]["heard"] is True
    assert facts["discovery"]["callGeneration"] == "0"
    assert any(p["type"] == "meetings" and any(m["heard"] for m in p["meetings"]) for p in facts["packets"])
    return facts["packets"]


def test_passive_rtp_never_reaches_automatic_start(tmp_path):
    assert replay(packets(), tmp_path) == []


@pytest.mark.parametrize(("automatic", "active"), [(False, True), (True, False)])
def test_existing_opt_in_and_application_gates(tmp_path, automatic, active):
    assert replay(packets(), tmp_path, automatic=automatic, active=active) == []


def test_passive_rtp_remains_available_for_manual_start(tmp_path):
    starts = replay(packets(), tmp_path, automatic=False, manual=True)
    assert len(starts) == 1
    assert starts[0]["automatic"] is False
    assert starts[0]["mode"] == "tracks"


def test_previously_queued_start_revalidates_at_dispatch(tmp_path):
    def queued(service, browser):
        tab = next(iter(browser.meetings))
        identifier = service._meeting_id(browser, tab)
        service.workers.submit(service._automatic, identifier, "start", service._stop_generation)

    assert replay(packets(), tmp_path, before_dispatch=queued) == []


def test_legacy_capabilities_cannot_restore_playback_auto_start(tmp_path):
    wire = packets()
    hello = next(packet for packet in wire if packet["type"] == "hello")
    hello["capabilities"].remove("call-evidence-v1")
    assert replay(wire, tmp_path) == []
