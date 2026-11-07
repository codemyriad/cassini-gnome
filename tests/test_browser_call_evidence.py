# SPDX-License-Identifier: GPL-3.0-or-later
"""D30: connected receive slots are listed, but are not automatic start evidence."""

import io
import json
from pathlib import Path

import pytest

from cassini_talk.browser_service import BrowserConnection, BrowserService
from cassini_talk.session_events import source_state

SHARED_AUTO_START_CASES = json.loads(
    (Path(__file__).parents[1] / "browser-protocol/conformance/control/auto-start-vectors.json").read_text()
)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    service = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    browser = BrowserConnection(io.BytesIO())
    service.browsers[browser.id] = browser
    service.update(browser, dict(type="hello", version=1, capabilities=["call-evidence-v1", "independent-sessions-v1"]))
    service.update(browser, dict(type="status", recording=False))
    tasks = []
    monkeypatch.setattr(service.workers, "submit", lambda fn, *args: tasks.append((fn, args)))
    service.auto_record = True
    yield service, browser, tasks
    service.workers.shutdown(wait=True)


def meeting(**extra):
    # Generated D30 topology: three live recvonly slots and a sendonly preview mic.
    # Protocol discovery deliberately reports receivers only.
    return dict(tabId=3, title="Pre-join fixture", url="https://meet.example/test", sourceCount=3, **extra)


@pytest.mark.parametrize("heard", [False, None, "true", 1, {}, []])
def test_capable_companion_lists_prejoin_but_does_not_start(policy, heard):
    service, browser, tasks = policy
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=heard)]))
    assert len(service.snapshot()["meetings"]) == 1
    assert not service.snapshot()["meetings"][0]["heard"]
    assert not tasks and not browser.attempted
    service.policy()
    assert not tasks


def test_capable_companion_requires_positive_count_and_explicit_heard(policy):
    service, browser, tasks = policy
    service.update(browser, dict(type="meetings", meetings=[meeting()]))
    assert not tasks
    service.update(browser, dict(type="meetings", meetings=[dict(meeting(heard=True), sourceCount=0)]))
    assert not tasks
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    assert len(tasks) == 1 and tasks[0][1][1] == "start"
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    assert len(tasks) == 1, "replayed evidence does not start twice"


@pytest.mark.parametrize("capabilities", [[], ["independent-sessions-v1"]])
def test_legacy_companion_keeps_previous_auto_start(policy, capabilities):
    service, browser, tasks = policy
    service.update(browser, dict(type="hello", version=1, capabilities=capabilities))
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=False)]))
    assert len(tasks) == 1 and tasks[0][1][1] == "start"


def test_queued_auto_start_rechecks_evidence_and_can_retry_when_joined(policy, monkeypatch):
    service, browser, tasks = policy
    commands = []
    monkeypatch.setattr(service, "command", lambda *args: commands.append(args))
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    fn, args = tasks.pop()
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=False)]))
    fn(*args)
    assert not commands and not browser.attempted and not browser.scheduled
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    assert len(tasks) == 1


def test_manual_record_and_record_all_do_not_require_heard(policy, monkeypatch):
    service, browser, tasks = policy
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=False)]))
    commands = []

    def send(packet):
        commands.append(packet)
        service.update(
            browser, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
        )

    monkeypatch.setattr(browser, "send", send)
    service.command(f"{browser.id}:3", "start")
    result = service.start_all()
    assert result["attempted"] == 1 and not result["failures"]
    assert [packet["action"] for packet in commands] == ["start", "start"]
    assert not tasks


def test_each_independent_meeting_needs_its_own_evidence(policy):
    service, browser, tasks = policy
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=False), dict(meeting(heard=True), tabId=4)]))
    assert [args[0] for _, args in tasks] == [f"{browser.id}:4"]
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True), dict(meeting(heard=True), tabId=4)]))
    assert [args[0] for _, args in tasks] == [f"{browser.id}:4", f"{browser.id}:3"]


def test_rejected_auto_start_cannot_fall_back_after_evidence_disappears(policy, monkeypatch):
    from cassini_talk.browser_service import StartRejected

    service, browser, tasks = policy
    fallbacks = []

    def reject(*_args, **_kwargs):
        service.update(browser, dict(type="meetings", meetings=[meeting(heard=False)]))
        raise StartRejected("Generated source disappeared during Start")

    monkeypatch.setattr(service, "command", reject)
    monkeypatch.setattr(service, "start_fallback", lambda *args, **kwargs: fallbacks.append(args))
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    fn, args = tasks.pop()
    fn(*args)
    assert not fallbacks and not browser.attempted
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=True)]))
    assert len(tasks) == 1, "a later genuine Join remains eligible"


def test_auto_fallback_reservation_rechecks_capable_evidence(policy, monkeypatch):
    from cassini_talk import browser_capture

    service, browser, _tasks = policy
    service.update(browser, dict(type="meetings", meetings=[meeting(heard=False)]))
    monkeypatch.setattr(
        browser_capture,
        "BrowserCapture",
        lambda *_args, **_kwargs: pytest.fail("constructed a pre-join native capture"),
    )
    with pytest.raises(ValueError, match="cancelled"):
        service.start_fallback(f"{browser.id}:3", meeting(heard=True), automatic_generation=service._stop_generation)
    assert not service.captures


@pytest.fixture
def command_packets(policy, monkeypatch):
    """Acknowledge transport commands on every peer; keep production policy/dispatch intact."""
    service, _browser, _tasks = policy
    packets = []

    def send(peer, packet):
        packets.append(packet)
        service.update(
            peer,
            dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False)),
        )

    monkeypatch.setattr(BrowserConnection, "send", send)
    return packets


def seed_recordings(service, browser, vector, parent):
    """Seed D52.5 system/lost recordings into the real registry; return the vector's peer.

    Each recording is started the production way: its peer reports the call and
    the capture, then the audio transport closes (capture.close() falls back to
    system/lost). `controlPeer: earlier` closes that peer afterwards, so the
    vector's hello and meetings arrive on a new peer of the same browser.
    """
    import time

    from test_browser_capture import Tap

    from cassini_talk.browser_capture import BrowserCapture

    recordings = vector.get("recordings", [])
    if not recordings:
        return browser
    peer = None
    for recording in recordings:
        assert recording["othersSource"] == dict(source="system", reason="lost")
        capture = BrowserCapture(service._capture_changed, parent=parent, tap_factory=Tap)
        capture.start(
            dict(
                type="start",
                captureId=recording["captureId"],
                callGeneration=recording["callGeneration"],
                timelineOriginMs=int(time.time() * 1000),
                microphone=False,
            )
        )
        service.captures[recording["captureId"]] = capture
        peer = BrowserConnection(None, browser_instance="auto-start-browser")
        service.browsers[peer.id] = peer
        tab = recording["tabId"]
        row = dict(
            vector["meeting"],
            tabId=tab,
            callGeneration=recording["callGeneration"],
            heard=recording.get("heardSeen", False),
        )
        service.update(peer, dict(type="hello", version=1, capabilities=vector["capabilities"]))
        service.update(peer, dict(type="meetings", meetings=[row]))
        session = dict(recording=True, tabId=tab, captureId=recording["captureId"], pageState="present")
        service.update(
            peer, dict(type="status", recording=True, sessions=[dict(session, callGeneration=row["callGeneration"])])
        )
        capture.close()
        assert capture.browser_lost and not capture.finished
        # The extension saw its native port close and deleted that session
        # (background.js stop()); until then it would refuse a resume Start.
        service.update(peer, dict(type="status", recording=False, sessions=[]))
        if recording.get("controlPeer", "current") == "earlier":
            with service.lock:
                peer.closed = True
                service.browsers.pop(peer.id)
            peer = None
    service.browsers.pop(browser.id, None)
    if peer is None:
        peer = BrowserConnection(None, browser_instance="auto-start-browser")
        service.browsers[peer.id] = peer
        service.update(peer, dict(type="status", recording=False, sessions=[]))
    return peer


def row(vector):
    """The vector's meeting row, sent exactly as written (it carries pageState, D57)."""
    return vector["meeting"]


def resume_packets(packets):
    return [packet for packet in packets if "resumeCapture" in packet]


def drop_automatic(tasks, service):
    tasks[:] = [item for item in tasks if item[0] != service._automatic]


def starts(tasks, service):
    return [arguments for function, arguments in tasks if function == service._automatic and arguments[1] == "start"]


def settle(service, tasks):
    """Run queued production work other than automatic starts, as the worker pool
    would (a call boundary finalizes the old call's recording there), then let
    policy reconsider."""
    while work := [(function, arguments) for function, arguments in tasks if function != service._automatic]:
        for item in work:
            tasks.remove(item)
            item[0](*item[1])
    service.policy()


@pytest.mark.parametrize("vector", SHARED_AUTO_START_CASES, ids=lambda case: case["name"])
def test_shared_auto_start_vectors_schedule_and_dispatch(policy, command_packets, vector, tmp_path):
    service, browser, tasks = policy
    service.auto_record = False
    browser = seed_recordings(service, browser, vector, tmp_path / "Library")
    service.auto_record = True
    # Seeding queues no automatic Start (auto-record is off). Its lifecycle
    # work, such as a resume ask once the extension released the tab, stays
    # queued for the worker pool, as it would in production.
    drop_automatic(tasks, service)
    service.update(browser, dict(type="hello", version=1, capabilities=vector["capabilities"]))
    service.update(browser, dict(type="meetings", meetings=[row(vector)]))
    settle(service, tasks)
    scheduled = starts(tasks, service)
    assert bool(scheduled) is vector["expect"]["automatic"]
    if scheduled:
        assert len(scheduled) == 1
        service._automatic(*scheduled[0])
    # settle() also ran the lifecycle's one resume ask; it is not a Start.
    resumes = resume_packets(command_packets)
    assert len(resumes) == (1 if vector["expect"].get("resume") else 0)
    started = [packet for packet in command_packets if packet["action"] == "start" and packet not in resumes]
    assert bool(started) is vector["expect"]["automatic"]
    for packet in started:
        assert packet["tabId"] == vector["meeting"]["tabId"]
        assert packet["independent"] is True and "resumeCapture" not in packet
    tasks.clear()
    service.policy()
    assert not starts(tasks, service), "replayed inventory cannot duplicate a scheduled start"


@pytest.mark.parametrize("vector", SHARED_AUTO_START_CASES, ids=lambda case: case["name"])
def test_shared_auto_start_vectors_resume_a_lost_recording_once(policy, command_packets, vector, tmp_path):
    service, browser, tasks = policy
    service.auto_record = False
    browser = seed_recordings(service, browser, vector, tmp_path / "Library")
    service.auto_record = True
    service.update(browser, dict(type="hello", version=1, capabilities=vector["capabilities"]))
    service.update(browser, dict(type="meetings", meetings=[row(vector)]))
    # Replayed inventory, a re-hello on the same peer and repeated ticks: the
    # worker pool runs the queued automatic and lifecycle work each time.
    for _ in range(3):
        service.policy()
        while work := [item for item in tasks if item[0] in (service._automatic, service._capture_lifecycle)]:
            for item in work:
                tasks.remove(item)
                item[0](*item[1])
        service.update(browser, dict(type="meetings", meetings=[row(vector)]))
        service.update(browser, dict(type="hello", version=1, capabilities=vector["capabilities"]))
    resumes = [packet["resumeCapture"]["captureId"] for packet in resume_packets(command_packets)]
    expected = [r["captureId"] for r in vector.get("recordings", [])][:1] if vector["expect"].get("resume") else []
    assert resumes == expected
    fresh = [packet for packet in command_packets if packet["action"] == "start" and "resumeCapture" not in packet]
    assert not fresh or vector["expect"]["automatic"], "a lost call is never started a second time"
    for packet in resume_packets(command_packets):
        capture = service.captures[packet["resumeCapture"]["captureId"]]
        assert packet == dict(
            type="command",
            requestId=packet["requestId"],
            action="start",
            mode="tracks",
            tabId=vector["meeting"]["tabId"],
            independent=True,
            captureLocal=False,
            callGeneration=vector["meeting"]["callGeneration"],
            keepOnNavigation=True,
            resumeCapture=dict(
                captureId=capture.capture_id,
                timelineOriginMs=capture.origin_ms,
                lostAtMs=source_state(capture.host.session.manifest["events"]).since_ms,
            ),
        )
        assert capture.browser_lost and not capture.finished


@pytest.mark.parametrize("vector", SHARED_AUTO_START_CASES, ids=lambda case: case["name"])
def test_shared_manual_start_vectors_use_record_candidates_and_dispatch(policy, command_packets, vector, tmp_path):
    service, browser, tasks = policy
    service.auto_record = False
    browser = seed_recordings(service, browser, vector, tmp_path / "Library")
    drop_automatic(tasks, service)
    service.update(browser, dict(type="hello", version=1, capabilities=vector["capabilities"]))
    service.update(browser, dict(type="meetings", meetings=[row(vector)]))
    settle(service, tasks)
    command_packets[:] = [packet for packet in command_packets if "resumeCapture" not in packet]
    # Record uses the production candidate filter; zero-source entries are not
    # candidates. Explicit starts for eligible tabs must also bypass heard.
    result = service.start_all()
    assert bool(result["attempted"]) is vector["expect"]["manual"]
    assert not result["failures"] and not starts(tasks, service)
    assert len(command_packets) == result["attempted"]
    if result["attempted"]:
        service.command(f"{browser.id}:{vector['meeting']['tabId']}", "start")
        assert len(command_packets) == 2
    elif vector.get("recordings"):
        # D52.5: the row shows Stop, and an explicit Start of that call is
        # refused before anything reaches the browser.
        with pytest.raises(ValueError):
            service.command(f"{browser.id}:{vector['meeting']['tabId']}", "start", user=True)
        assert not command_packets
    for packet in command_packets:
        assert packet["action"] == "start" and packet["tabId"] == vector["meeting"]["tabId"]
        assert packet["independent"] is True
