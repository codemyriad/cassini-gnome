# SPDX-License-Identifier: GPL-3.0-or-later
"""D74.1: evidence owns association lifetimes; audio never does."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassini_talk.call_signals import SignalCalls, rows


def peer(name="first", stamp=1, instance="private-profile"):
    return SimpleNamespace(id=name, connected_at=stamp, browser_instance=instance, signals_instance=instance)


def call(tab=3, generation="call-A", document="document-A", state="active", remote="mixed", reason=None):
    return dict(
        tabId=tab,
        url="https://meeting.example/test",
        documentGeneration=document,
        remoteAudio=remote,
        callEvidence=dict(
            state=state,
            reason=reason or {"active": "activeCall", "unknown": "unverified", "inactive": "callEnded"}[state],
            adapter=dict(name="Synthetic meeting", version="1"),
            callGeneration=generation,
        ),
    )


def update(model, owner, calls, now=0, enabled=True):
    _sequence, snapshot = rows(dict(browserInstance=owner.signals_instance, sequence=1, tabs=calls))
    model.update(owner, snapshot, now)
    model.advance(now, enabled=enabled)


@pytest.mark.parametrize(
    "remote,state,enabled",
    [
        ("mixed", "unknown", True),
        ("mixed", "inactive", True),
        ("mixed", "active", False),
        ("separated", "active", True),
        ("unknown", "active", True),
    ],
)
def test_no_admission_from_playback_classification_separated_or_opt_out(remote, state, enabled):
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call(remote=remote, state=state)], enabled=enabled)
    assert model.calls == {} and model.capture_id is None


def test_shared_references_fixed_route_and_positive_end_with_continued_media():
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call()])
    model.reserve("one-system-capture")
    model.started()
    update(model, owner, [call(remote="separated"), call(tab=4, generation="call-B")])
    assert len(model.calls) == 2 and model.capture_id == "one-system-capture"
    update(model, owner, [call(state="inactive"), call(tab=4, generation="call-B")])
    assert len(model.calls) == 1 and model.retiring is None
    update(model, owner, [call(state="inactive"), call(tab=4, generation="call-B", state="inactive")])
    assert not model.calls and model.retiring == "callEnded"


def test_reconnect_does_not_duplicate_and_old_end_cannot_stop_replacement():
    model, old, new = SignalCalls(), peer(), peer("replacement", 2)
    model.claim(old, 0)
    update(model, old, [call()])
    model.reserve("shared")
    model.started()
    model.disconnect(old, 1)
    model.claim(new, 2)
    update(model, new, [call()], 3)
    update(model, old, [call(state="inactive")], 4)
    model.disconnect(old, 5)
    assert model.capture_id == "shared" and model.retiring is None
    assert next(iter(model.calls.values())).lost_at is None


@pytest.mark.parametrize("loss", ["unknown", "removed", "disconnect"])
def test_evidence_loss_stops_at_120_seconds_and_is_not_call_end(loss):
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call()])
    model.reserve("capture")
    model.started()
    if loss == "disconnect":
        model.disconnect(owner, 10)
    else:
        update(model, owner, [call(state="unknown")] if loss == "unknown" else [], 10)
    for now in (40, 90, 129):
        model.advance(now, enabled=True)
        assert model.retiring is None
    model.advance(130, enabled=True)
    assert model.retiring == "pageGone" and not model.calls


def test_manual_stop_suppresses_old_call_generation_but_not_the_next():
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call(), call(tab=4, generation="call-B")])
    model.reserve("shared")
    model.started()
    model.suppress()
    model.released()
    update(model, owner, [call(document="reloaded"), call(tab=4, generation="call-B")])
    assert not model.calls
    update(model, owner, [call(generation="call-C")])
    assert len(model.calls) == 1 and next(iter(model.calls))[3] == "call-C"


def test_generation_fence_cannot_be_coalesced_into_a_continuing_old_capture():
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call()])
    model.reserve("old")
    model.started()
    update(model, owner, [call(generation="call-B")])
    assert model.retiring == "callEnded" and not model.calls
    model.released()
    model.advance(1, enabled=True)
    assert next(iter(model.calls))[3] == "call-B"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(remoteAudio="invented"),
        lambda r: r["callEvidence"].update(adapter=None),
        lambda r: r["callEvidence"].update(callGeneration=None),
        lambda r: r["callEvidence"].update(reason="audioPlaying"),
        lambda r: r.update(documentGeneration="é" * 101),
    ],
)
def test_invalid_evidence_cannot_become_a_candidate(change):
    row = call()
    change(row)
    with pytest.raises(ValueError):
        rows(dict(browserInstance="private-profile", sequence=0, tabs=[row]))


def test_superseded_generation_end_starts_grace_without_replacing_or_extending_it():
    model, owner = SignalCalls(), peer()
    model.claim(owner, 0)
    update(model, owner, [call(generation="call-B")])
    model.reserve("current")
    model.started()
    update(model, owner, [call(state="inactive")], 1)
    assert model.retiring is None and next(iter(model.calls.values())).lost_at == 1
    assert next(iter(model.calls))[3] == "call-B" and model.capture_id == "current"
    update(model, owner, [call(generation="call-B", state="unknown")], 2)
    update(model, owner, [call(state="inactive")], 3)
    assert next(iter(model.calls.values())).lost_at == 1
    model.advance(120, enabled=True)
    assert model.retiring is None
    model.advance(121, enabled=True)
    assert model.retiring == "pageGone" and not model.calls


# The merged contract is replayed through GNOME's parser and association policy,
# alongside the real broker/native-source proofs in test_call_signals_capture.py.

SHARED = json.loads(
    (Path(__file__).parents[1] / "browser-protocol/conformance/control/call-signals-vectors.json").read_text()
)


@pytest.mark.parametrize(
    "case", [c for c in SHARED["valid"] if c["message"]["type"] == "callSignals"], ids=lambda c: c["name"]
)
def test_shared_accepted_signal_shapes(case):
    rows(case["message"])


@pytest.mark.parametrize(
    "case", [c for c in SHARED["invalid"] if c["message"]["type"] == "callSignals"], ids=lambda c: c["name"]
)
def test_shared_refused_signal_shapes(case):
    with pytest.raises(ValueError):
        rows(case["message"])


@pytest.mark.parametrize("case", SHARED["routing"] + SHARED["passiveMediaNegatives"], ids=lambda c: c["name"])
def test_shared_routing_and_passive_media_use_gnome_admission(case):
    from cassini_talk.browser_service import BrowserService

    message = case["signal"]
    model, owner = SignalCalls(), peer(instance=message["browserInstance"])
    model.claim(owner, 0)
    _seq, snapshot = rows(message)
    model.update(owner, snapshot, 0)
    model.advance(0, enabled=case["autoRecordOptIn"])
    assert bool(model.calls) == case["expect"]["appSystemStart"]
    owner.signal_rows = snapshot
    owner.capabilities = {"call-evidence-v1"}
    rtp = case.get("rtp", dict(sourceCount=0, heard=False))
    legacy = (
        case["autoRecordOptIn"]
        and BrowserService._auto_start_ready(owner, rtp)
        and not BrowserService._signal_blocks_browser(SimpleNamespace(signal_calls=model), owner, 2, rtp)
    )
    assert bool(legacy) == case["expect"]["browserStart"]


def _shared_row(association, state="active", remote="mixed"):
    row = copy.deepcopy(next(c["message"]["tabs"][0] for c in SHARED["valid"] if c["name"] == "positive-mixed-call"))
    row.update(tabId=association["tabId"], documentGeneration=association["documentGeneration"], remoteAudio=remote)
    row["callEvidence"].update(
        state=state,
        reason={"active": "activeCall", "inactive": "callEnded", "unknown": "unverified"}[state],
        callGeneration=association["callGeneration"],
    )
    return row


def test_shared_reference_count_sequence():
    model, owner = SignalCalls(), peer(instance="profile-A")
    model.claim(owner, 0)
    current = {}
    starts = 0
    for step in SHARED["appPolicy"]["sharedOwner"]["steps"]:
        association = step["association"]
        current[association["tabId"]] = _shared_row(association, "active" if step["event"] == "active" else "inactive")
        update(model, owner, list(current.values()))
        if model.calls and model.capture_id is None:
            starts += 1
            model.reserve("shared")
            model.started()
        assert len(model.calls) == step["expect"]["associatedCalls"]
        assert starts == step["expect"]["systemStarts"]
        assert bool(model.retiring) == step["expect"]["stop"]


def test_shared_loss_deadline_is_never_extended_by_other_evidence_loss():
    from cassini_talk.call_signals import LOSS_GRACE

    case = SHARED["appPolicy"]["lossDeadline"]
    model, owner = SignalCalls(), peer(instance=case["association"]["browserInstance"])
    model.claim(owner, 0)
    update(model, owner, [_shared_row(case["association"])])
    model.reserve("owned")
    model.started()
    for step in case["steps"]:
        now = step["atMs"] / 1000
        if step["event"] == "evidenceUnknown":
            update(model, owner, [_shared_row(case["association"], "unknown")], now)
        elif step["event"] == "rowRemoved":
            update(model, owner, [], now)
        elif step["event"] == "controlLost":
            model.disconnect(owner, now)
        model.advance(now, enabled=True)
        assert bool(model.retiring) == step["expect"]["stop"]
        if model.calls:
            assert (next(iter(model.calls.values())).lost_at + LOSS_GRACE) * 1000 == step["expect"]["deadlineMs"]
