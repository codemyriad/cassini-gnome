# SPDX-License-Identifier: GPL-3.0-or-later
"""D72 diagnostics and per-profile health never become recording policy."""

import io

import pytest

from cassini_talk import browser_health as H
from cassini_talk.browser_connection import BrowserConnection as Controller
from cassini_talk.browser_service import BrowserConnection, BrowserService

CAPS = ["multi-stream-v1", "tab-readiness-v1"]


def status(state="idle", tabs=None):
    return dict(state=state, tabs=[] if tabs is None else tabs, worker="ready", nativeControl="connected")


def tab(number=7, state="reloadRequired", **extra):
    return dict(tabId=number, state=state, action="reloadMeetingTab", hookSeen=True, relayAlive=False, **extra)


def peer(**extra):
    return dict(
        id="peer-1",
        browser="Chrome",
        capabilities=CAPS,
        diagnostics=dict(filesOk=True),
        browserHealth=status(),
        busyTabs=[],
        **extra,
    )


def test_diagnostics_reset_self_check_on_a_new_version_or_extension_instance():
    before = H.diagnostics(
        {}, dict(filesOk=True, extensionVersion="0.2.18", extensionInstance="old", installType="development")
    )
    assert H.diagnostics(before, {}) == before
    for key, new in [("extensionVersion", "0.2.20"), ("extensionInstance", "new")]:
        after = H.diagnostics(before, {key: new})
        assert "filesOk" not in after and "installType" not in after
    assert H.diagnostics(before, dict(filesOk=1))["filesOk"] is None


@pytest.mark.parametrize("check", [True, None, 0, 1, "true", "absent"])
def test_unknown_missing_or_malformed_diagnostics_do_not_block_done(check):
    raw = peer()
    if check == "absent":
        raw["diagnostics"].pop("filesOk")
    else:
        raw["diagnostics"]["filesOk"] = check
    assert H.setup_complete(H.read_peers([raw]))
    assert H.setup_complete(())  # A connected legacy broker can omit peer diagnostics.


def test_only_an_explicit_failed_file_check_blocks_done():
    raw = peer()
    raw["diagnostics"]["filesOk"] = False
    assert not H.setup_complete(H.read_peers([raw]))


def test_every_connected_profile_must_pass_and_version_is_not_feature_gating():
    a, b = peer(), peer()
    b["id"] = "second"
    a["diagnostics"]["extensionVersion"] = "0.2.2"
    assert H.setup_complete(H.read_peers([a, b]))
    b["diagnostics"]["filesOk"] = False
    assert not H.setup_complete(H.read_peers([a, b]))
    b["diagnostics"]["filesOk"] = True
    b["capabilities"] = ["tab-readiness-v1"]
    assert not H.setup_complete(H.read_peers([a, b]))


@pytest.mark.parametrize(
    "reported,bundled,expected",
    [
        ("0.2.18", "0.2.20", True),
        ("0.2.20.0", "0.2.20", False),
        ("0.3", "0.2.20", False),
        (None, "0.2.20", False),
        ("future", "0.2.20", False),
        ("０.２", "0.2.20", False),
    ],
)
def test_version_comparison_is_diagnostic_only(reported, bundled, expected):
    assert H.older(reported, bundled) is expected


def test_idle_unknown_and_unrelated_tabs_never_manufacture_reload_guidance():
    for value in [None, status(), status("idle", [tab()]), status("ready", [tab(7, "ready")])]:
        raw = peer()
        raw["browserHealth"] = value
        (value,) = H.read_peers([raw])
        assert H.reload_tabs(value) == ()
    raw = peer()
    raw["browserHealth"] = status("reloadRequired", [tab(3, "ready"), tab(7)])
    (value,) = H.read_peers([raw])
    assert [t["tabId"] for t in H.reload_tabs(value)] == [7]
    assert H.can_reload(value, H.reload_tabs(value)[0])
    value["busyTabs"] = [7]
    assert not H.can_reload(value, H.reload_tabs(value)[0])
    value["busyTabs"] = []
    value["capabilities"] = ["multi-stream-v1"]
    assert not H.can_reload(value, H.reload_tabs(value)[0])


@pytest.mark.parametrize("tabs", [[tab(True)], [tab(-1)], [tab(), tab()], [tab(state="future")], [tab()] * 101])
def test_malformed_health_is_unknown(tabs):
    assert H.health(status("reloadRequired", tabs)) is None


@pytest.fixture
def service(tmp_path):
    value = BrowserService(tmp_path / "config.json", notifier=lambda *_: None, command_timeout=0.1)
    yield value
    value.workers.shutdown(wait=True)


def attach(service, number="one"):
    browser = BrowserConnection(io.BytesIO(), browser_name="Chrome")
    service.browsers[browser.id] = browser
    service.update(
        browser,
        dict(
            type="hello",
            version=1,
            capabilities=CAPS,
            filesOk=True,
            extensionVersion="0.2.20",
            extensionInstance=number,
        ),
    )
    service.update(browser, dict(type="status", recording=False, browserHealth=status("reloadRequired", [tab()])))
    return browser


def test_broker_preserves_diagnostics_and_producer_health_without_inventing_meetings(service):
    browser = attach(service)
    (value,) = service.snapshot()["browserPeers"]
    assert value["diagnostics"]["filesOk"] is True
    assert value["diagnostics"]["extensionVersion"] == "0.2.20"
    assert value["browserHealth"]["tabs"][0]["tabId"] == 7
    assert service.snapshot()["meetings"] == [] and not service.captures
    service.update(browser, dict(type="status", recording=False, browserHealth=status()))
    assert H.reload_tabs(service.snapshot()["browserPeers"][0]) == ()


def test_reload_is_explicit_profile_scoped_and_cannot_replay_stale_health(service):
    first, second = attach(service), attach(service, "two")
    commands = []
    first.send = lambda packet: pytest.fail("Wrong browser/profile")

    def send(packet):
        commands.append(packet)
        service.update(second, dict(type="status", recording=False, browserHealth=status("ready", [tab(7, "ready")])))
        service.update(
            second,
            dict(
                type="result",
                requestId=packet["requestId"],
                ok=True,
                status=dict(recording=False, browserHealth=status("reloadRequired", [tab()])),
            ),
        )

    second.send = send
    assert not commands
    result = service.reload_tab(second.id, 7)
    assert len(commands) == 1 and commands[0]["action"] == "reloadTab" and commands[0]["independent"] is True
    value = next(p for p in result["browserPeers"] if p["id"] == second.id)
    assert value["browserHealth"]["state"] == "ready"
    assert not service.captures and not service._pending_calls and not service._attempted_calls


@pytest.mark.parametrize("guard", ["idle", "capability", "recording", "stopping", "starting", "no-action"])
def test_reload_admission_rechecks_health_capability_and_busy_state(service, guard):
    browser = attach(service)
    browser.send = lambda _: pytest.fail("A guarded reload was dispatched")
    if guard == "idle":
        browser.browser_health = status()
    elif guard == "capability":
        browser.capabilities.discard("tab-readiness-v1")
    elif guard in ("recording", "stopping"):
        browser.states[7] = {guard: True}
    elif guard == "starting":
        browser.pending["start"] = {"tabId": 7}
    else:
        browser.browser_health["tabs"][0]["action"] = None
    with pytest.raises(ValueError):
        service.reload_tab(browser.id, 7)


def test_setup_controller_notifies_on_health_changes_and_drops_disconnected_peers():
    changed = []
    controller = Controller(
        lambda: changed.append(True), later=lambda *_: 1, post=lambda *_: None, cancel=lambda *_: None
    )
    snapshot = dict(connectedBrowsers=1, browserNames=["Chrome"], browserPeers=[peer()])
    controller.observe(snapshot)
    count = len(changed)
    controller.observe(snapshot)
    assert len(changed) == count
    snapshot["browserPeers"][0]["diagnostics"]["filesOk"] = False
    controller.observe(snapshot)
    assert len(changed) == count + 1 and not H.setup_complete(controller.snapshot()["peers"])
    controller.observe(dict(connectedBrowsers=0, browserNames=[]))
    assert controller.snapshot()["peers"] == ()
    controller.close()


def test_explicit_reload_round_trip_uses_the_control_wire_and_waits_for_fresh_health(service, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from broker_fixture import stop_broker

    from cassini_talk.browser_service import connect, read_packet, request, send_packet

    service.command_timeout = 2  # This is a protocol-ordering proof, not a latency benchmark.
    path = tmp_path / "private/browser.sock"
    ready, received = threading.Event(), threading.Event()
    update = service.update

    def observed(browser, packet):
        result = update(browser, packet)
        if packet.get("type") == "status":
            received.set()
        return result

    monkeypatch.setattr(service, "update", observed)
    server = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    server.start()
    assert ready.wait(2)
    try:
        with connect(path, timeout=2) as connection, connection.makefile("rwb") as stream:
            send_packet(stream, dict(role="extension", browserName="Chrome"))
            send_packet(stream, dict(type="hello", version=1, capabilities=CAPS, filesOk=True))
            send_packet(stream, dict(type="status", recording=False, browserHealth=status("reloadRequired", [tab()])))
            assert received.wait(2)
            (profile,) = request(path=path)["browserPeers"]
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(request, "reload-tab", path=path, browserId=profile["id"], tabId=7)
                command = read_packet(stream)
                assert command["action"] == "reloadTab" and command["tabId"] == 7 and command["independent"] is True
                send_packet(stream, dict(type="result", requestId=command["requestId"], ok=True))
                snapshot = future.result(timeout=3)
            assert snapshot["browserPeers"][0]["browserHealth"]["state"] == "reloadRequired"
            received.clear()
            send_packet(stream, dict(type="status", recording=False, browserHealth=status()))
            assert received.wait(2)
            assert H.reload_tabs(request(path=path)["browserPeers"][0]) == ()
            assert not service.captures and not service._pending_calls
    finally:
        stop_broker(service, server, path)
