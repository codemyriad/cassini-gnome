# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual broker RPC: healthy audio survives its control worker rediscovery."""

import time
from dataclasses import replace
from uuid import uuid4

from test_browser_capture import bind_capture, remote, server, start  # noqa: F401
from test_browser_service import meeting, wait_for
from test_browser_sessions import Extension

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
from cassini_viewer.browser_service import connect, request, send_packet
from cassini_viewer.call_lifecycle import CALL_END_GRACE, CONTROL_RECONNECT_GRACE

INSTANCE = "4242:99"
CAPABILITIES = ["independent-sessions-v1", "call-evidence-v1", "page-state-v1"]


def peer(path, tabs=(3,), instance=INSTANCE):
    extension = Extension.__new__(Extension)
    extension.path = path
    extension.connection = connect(path, timeout=2)
    extension.stream = extension.connection.makefile("rwb")
    extension.sessions = {}
    extension.title = "Control peer " + str(uuid4())
    send_packet(extension.stream, dict(role="extension", browserName="Chrome", browserInstance=instance))
    send_packet(extension.stream, dict(type="hello", version=1, capabilities=CAPABILITIES))
    send_packet(extension.stream, dict(type="status", recording=False, sessions=[]))
    inventory(extension, tabs)
    return extension


def inventory(extension, tabs, page="present", count=2):
    send_packet(
        extension.stream,
        dict(
            type="meetings",
            meetings=[
                dict(meeting(tab), title=extension.title, pageState=page, sourceCount=count, heard=count > 0)
                for tab in tabs
            ],
        ),
    )
    wait_for(
        lambda: len([m for m in request(path=extension.path)["meetings"] if m["title"] == extension.title]) == len(tabs)
    )


def live(service, path):
    extension = peer(path)
    client = BrowserAudioProxy(path=path)
    start(client, "retained")
    remote(client, "voice")
    capture = service.captures["retained"]
    bind_capture(extension, capture)
    wait_for(lambda: capture.lifecycle_capable)
    return extension, client, capture


def packet(client, sequence):
    client.handle(
        dict(
            type="audio", id="voice", sequence=sequence, offsetMs=200 + sequence * 4 * 1000 / 48000, pcm="AQACAAMABAA="
        )
    )


def lose(service, extension):
    extension.close()
    wait_for(lambda: not service.browsers)


def age(service, seconds, capture_id="retained"):
    # Advance a stored deadline, not the process/socket clock. Other tests
    # below also cross the real two-second policy threshold with real RPC.
    since = time.monotonic() - seconds
    service._capture_lifecycles[capture_id].missing = since
    if capture_id in getattr(service, "_control_reconnects", {}):
        reconnect = service._control_reconnects[capture_id]
        service._control_reconnects[capture_id] = replace(reconnect, since=since)


def test_real_control_gap_over_two_seconds_keeps_browser_pcm_mic_and_folder(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    folder, microphone = capture.directory, capture.microphone
    try:
        lose(service, extension)
        started = time.monotonic()
        sequence = 0
        # This is a duration assertion, not a startup sleep: PCM keeps arriving
        # through the actual host while the independent control socket is down.
        while time.monotonic() - started < CALL_END_GRACE + 0.3:
            sequence += 1
            packet(client, sequence)
            service.policy()
            assert not capture.browser_lost, "control-only reconnect moved healthy browser audio to system"
            time.sleep(0.02)
        extension = peer(path)
        wait_for(
            lambda: any(
                s.get("captureId") == "retained" and not s["id"].startswith("capture:")
                for s in request(path=path)["statuses"]
            )
        )
        assert not capture.browser_lost and "retained" not in service._control_reconnects
        assert capture.directory == folder and capture.microphone is microphone
        assert not any(t["kind"] == "system" for t in capture.host.session.manifest["tracks"])
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_grace_is_bounded_and_does_not_restart_the_two_minute_return_timer(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        age(service, CONTROL_RECONNECT_GRACE + 0.1)
        packet(client, 1)
        service.policy()
        wait_for(lambda: capture.browser_lost)
        assert not capture.finished
        age(service, 120.01)
        service.policy()
        wait_for(lambda: capture.finished)
        assert capture.host.session.manifest["events"][-1]["reason"] == "pageGone"
    finally:
        capture.stop()
        client.close()


def test_a_real_audio_eof_does_not_wait_for_control_reconnect_grace(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        client.close()
        wait_for(lambda: capture.browser_lost)
        assert not capture.finished and "system" in capture.native_taps
    finally:
        capture.stop()
        client.close()


def test_another_profile_or_browser_cannot_adopt_or_extend_this_captures_grace(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    others = []
    try:
        lose(service, extension)
        others = [peer(path, (7,)), peer(path, (3,), instance="5151:12")]
        packet(client, 1)
        service.policy()
        assert all(
            not any(s.get("captureId") == "retained" for s in b.states.values()) for b in service.browsers.values()
        )
        assert "retained" in service._control_reconnects
        age(service, CONTROL_RECONNECT_GRACE + 0.1)
        service.policy()
        wait_for(lambda: capture.browser_lost)
    finally:
        capture.stop()
        for extension in others:
            extension.close()
        client.close()


def test_late_tab_inventory_reattaches_only_its_own_capture(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        extension = peer(path, ())
        inventory(extension, (7,))
        age(service, 3)
        packet(client, 1)
        service.policy()
        assert not capture.browser_lost
        assert all(3 not in b.states for b in service.browsers.values())
        inventory(extension, (3, 7))
        wait_for(lambda: "retained" not in service._control_reconnects)
        state = next(iter(service.browsers.values()))
        assert state.states[3]["captureId"] == "retained" and 7 not in state.states
        assert not capture.browser_lost
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_retiring_old_control_after_replacement_does_not_start_a_new_gap(server):  # noqa: F811
    service, path, _ = server
    old, client, capture = live(service, path)
    old_id = next(iter(service.browsers))
    replacement = peer(path)
    try:
        wait_for(
            lambda: any(
                b.id != old_id and b.states.get(3, {}).get("captureId") == "retained" for b in service.browsers.values()
            )
        )
        old.close()
        wait_for(lambda: len(service.browsers) == 1)
        assert "retained" not in service._control_reconnects
        assert not capture.browser_lost
    finally:
        capture.stop()
        old.close()
        replacement.close()
        client.close()


def test_explicit_tab_close_keeps_two_second_stop_and_never_starts_system(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        extension = peer(path)
        inventory(extension, (3,), page="closed", count=0)
        wait_for(lambda: service._capture_lifecycles["retained"].closed is not None)
        service._capture_lifecycles["retained"].closed = time.monotonic() - CALL_END_GRACE - 0.1
        service.policy()
        wait_for(lambda: capture.finished)
        assert not any(t["kind"] == "system" for t in capture.host.session.manifest["tracks"])
        assert capture.host.session.manifest["events"][-1]["reason"] == "tabClosed"
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_an_open_audio_socket_without_fresh_pcm_does_not_get_the_control_only_grace(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        age(service, CALL_END_GRACE + 0.1)
        capture.streams.tracks["voice"].received_at = time.monotonic() - CALL_END_GRACE - 0.2
        assert capture.audio_connected and not capture.browser_ready
        service.policy()
        wait_for(lambda: capture.browser_lost)
    finally:
        capture.stop()
        client.close()


def test_user_stop_during_control_grace_is_kept_after_the_worker_returns(server):  # noqa: F811
    service, path, _ = server
    extension, client, capture = live(service, path)
    try:
        lose(service, extension)
        service.auto_record = True
        request("stop", path=path, id="capture:retained")
        assert capture.finished and "retained" not in service._control_reconnects
        extension = peer(path)
        assert 3 in next(iter(service.browsers.values())).attempted
        assert not next(iter(service.browsers.values())).pending
        assert service.stopped_calls[(INSTANCE, 3)][0] == meeting()["url"]
        assert capture.host.session.manifest["events"][-1]["reason"] == "user"
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_first_control_status_keeps_the_grace_even_without_a_second_status_after_audio_opens(server):  # noqa: F811
    service, path, _ = server
    extension = peer(path)
    client = BrowserAudioProxy(path=path)
    capture = None
    try:
        send_packet(
            extension.stream,
            dict(
                type="status",
                recording=True,
                sessions=[dict(tabId=3, recording=True, captureId="early", pageState="present")],
            ),
        )
        wait_for(lambda: any(s.get("captureId") == "early" for s in request(path=path)["statuses"]))
        start(client, "early")
        remote(client, "voice")
        capture = service.captures["early"]
        lose(service, extension)
        packet(client, 1)
        service.policy()
        assert capture.lifecycle_capable
        age(service, 3, "early")
        service.policy()
        assert not capture.browser_lost
    finally:
        if capture is not None:
            capture.stop()
        extension.close()
        client.close()
