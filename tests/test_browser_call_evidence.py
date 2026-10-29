# SPDX-License-Identifier: GPL-3.0-or-later
"""D30: connected receive slots are listed, but are not automatic start evidence."""

import io

import pytest

from cassini_viewer.browser_service import BrowserConnection, BrowserService


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
