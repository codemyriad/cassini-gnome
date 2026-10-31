# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassini_viewer.browser_service import BrowserService, connect, read_packet, request, send_packet


class SilentTap:
    """Native fallback input that never opens a real microphone or speaker."""

    def __init__(self, kind, origin, append, failed, *, level):
        pass

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "private/browser.sock"
    notifications = []

    def notify(title, stop):
        notifications.append((title, stop))
        return lambda: None

    # A rejected automatic start falls back to a native recording; keep it out
    # of the user's library and away from real audio devices.
    service = BrowserService(
        tmp_path / "settings.json",
        command_timeout=0.3,
        notifier=notify,
        capture_parent=tmp_path / "library",
        tap_factory=SilentTap,
    )
    service.notifications = notifications
    ready = threading.Event()
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    yield service, path
    service.closed.set()
    thread.join(2)
    assert not thread.is_alive()


def wait_for(check):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("Browser service did not reach expected state")


def meeting(tab=3):
    return dict(tabId=tab, title="Synthetic meeting", url="https://meeting.example/test", sourceCount=2)


def browser(path, instance=None, meetings=None):
    connection = connect(path)
    stream = connection.makefile("rwb")
    send_packet(stream, dict(role="extension", browserName="Chrome", browserInstance=instance))
    send_packet(stream, dict(type="hello", version=1))
    send_packet(stream, dict(type="status", recording=False, tabId=None))
    send_packet(stream, dict(type="meetings", meetings=[meeting()] if meetings is None else meetings))
    if meetings != []:
        wait_for(lambda: request(path=path)["meetings"])
    return connection, stream


def no_command(connection, stream):
    """True when the broker sends this browser nothing for a moment."""
    connection.settimeout(0.5)
    try:
        return read_packet(stream) is None
    except TimeoutError:
        return True
    finally:
        connection.settimeout(None)


def stop_automatic_call(path, stream):
    """Let automatic recording start tab 3, then stop it as the user would."""
    command = read_packet(stream)
    assert command["action"] == "start"
    send_packet(
        stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3))
    )
    wait_for(lambda: request(path=path)["meetings"][0]["recording"])
    identifier = request(path=path)["meetings"][0]["id"]
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(request, "stop", path=path, id=identifier)
        command = read_packet(stream)
        send_packet(stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False)))
        future.result(2)
    return identifier


def test_discovery_command_result_and_disconnect(service):
    _, path = service
    connection, stream = browser(path)
    try:
        listing = request(path=path)
        identifier = listing["meetings"][0]["id"]
        assert listing["browsers"] == 1 and not listing["autoRecord"]
        with ThreadPoolExecutor(1) as executor:
            result = executor.submit(request, "start", path=path, id=identifier)
            command = read_packet(stream)
            assert command["action"] == "start" and command["tabId"] == 3
            send_packet(
                stream,
                dict(
                    type="result",
                    requestId=command["requestId"],
                    ok=True,
                    status=dict(recording=True, tabId=3, path="/tmp/Recording-check"),
                ),
            )
            assert result.result(2)["meetings"][0]["recording"]
        with ThreadPoolExecutor(1) as executor:
            result = executor.submit(request, "stop", path=path, id=identifier)
            command = read_packet(stream)
            send_packet(
                stream,
                dict(
                    type="result",
                    requestId=command["requestId"],
                    ok=True,
                    status=dict(recording=False, path="/tmp/Recording-check", message="Saved"),
                ),
            )
            assert result.result(2)["recordings"][0]["path"] == "/tmp/Recording-check"
    finally:
        stream.close()
        connection.close()
    wait_for(lambda: request(path=path)["browsers"] == 0)


def test_auto_record_explicit_opt_in_persists_and_does_not_restart_after_stop(service):
    server, path = service
    connection, stream = browser(path)
    try:
        assert not request(path=path)["autoRecord"]
        request("configure", path=path, autoRecord=True)
        command = read_packet(stream)
        assert command["action"] == "start"
        send_packet(
            stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3))
        )
        wait_for(lambda: request(path=path)["meetings"][0]["recording"])
        assert BrowserService(server.config).auto_record
        wait_for(lambda: server.notifications)
        assert server.notifications[0][0] == "Synthetic meeting"
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(request, "stop", path=path, id=identifier)
            command = read_packet(stream)
            send_packet(
                stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False))
            )
            future.result(2)
        send_packet(stream, dict(type="meetings", meetings=[meeting()]))
        wait_for(lambda: not server.browsers[identifier.partition(":")[0]].busy)
        server.policy()
        assert server.browsers[identifier.partition(":")[0]].attempted == {3}
        # A genuinely new meeting on this tab is eligible again.
        send_packet(stream, dict(type="meetings", meetings=[]))
        wait_for(lambda: not request(path=path)["meetings"])
        send_packet(stream, dict(type="meetings", meetings=[meeting()]))
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def reconnect(path, connection, stream, instance="4242:99", meetings=None):
    connected = request(path=path)["browsers"]
    stream.close()
    connection.close()
    wait_for(lambda: request(path=path)["browsers"] == connected - 1)
    return browser(path, instance, meetings)


def test_reconnected_browser_does_not_restart_a_stopped_call(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    # The control link reconnects, as after an idle drop or an extension reload.
    connection, stream = reconnect(path, connection, stream)
    try:
        assert no_command(connection, stream), "the stopped call was started again"
    finally:
        stream.close()
        connection.close()


def test_restarted_extension_worker_reports_the_stopped_call_late(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    # A new worker knows no meeting until each page reconnects to it.
    connection, stream = reconnect(path, connection, stream, meetings=[])
    try:
        send_packet(stream, dict(type="meetings", meetings=[meeting(7)]))
        assert read_packet(stream)["tabId"] == 7
        send_packet(stream, dict(type="meetings", meetings=[meeting(7), meeting()]))
        wait_for(lambda: len(request(path=path)["meetings"]) == 2)
        assert no_command(connection, stream), "the stopped call was started again"
    finally:
        stream.close()
        connection.close()


def test_an_automatic_start_cut_off_by_a_disconnect_is_tried_again(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    assert read_packet(stream)["action"] == "start"
    # The link drops before the browser answers.
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_an_automatic_recording_lost_with_its_link_starts_again(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    command = read_packet(stream)
    send_packet(
        stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3))
    )
    wait_for(lambda: request(path=path)["meetings"][0]["recording"])
    # The worker dies with the recording; the new one reports none.
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_only_the_users_stop_is_remembered(service):
    server, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    command = read_packet(stream)
    send_packet(
        stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3))
    )
    identifier = wait_for(lambda: m["id"] if (m := request(path=path)["meetings"][0])["recording"] else None)
    # Cassini's own stop, for a call that ended or a browser that did not return.
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(server.command, identifier, "stop")
        command = read_packet(stream)
        send_packet(stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False)))
        future.result(2)
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_starting_a_stopped_call_again_forgets_the_stop(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    identifier = stop_automatic_call(path, stream)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(request, "start", path=path, id=identifier)
        command = read_packet(stream)
        send_packet(
            stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3))
        )
        future.result(2)
    # The worker dies with the recording the user started again.
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_quitting_cassini_does_not_count_as_stopping_the_call(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    command = read_packet(stream)
    send_packet(
        stream,
        dict(
            type="result",
            requestId=command["requestId"],
            ok=True,
            status=dict(recording=True, tabId=3, path="/tmp/Recording-quit"),
        ),
    )
    wait_for(lambda: request(path=path)["meetings"][0]["recording"])
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(request, "stop-all", path=path, quitting=True)
        command = read_packet(stream)
        send_packet(stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False)))
        future.result(2)
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_a_new_call_at_the_stopped_address_after_a_reconnect_is_new(service, monkeypatch):
    from cassini_viewer import browser_service

    _, path = service
    monkeypatch.setattr(browser_service, "STOPPED_CALL_REPORT_WINDOW", 0.3)
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    # The call ended while disconnected; later a call starts at the same address.
    connection, stream = reconnect(path, connection, stream, meetings=[])
    try:
        time.sleep(0.5)
        send_packet(stream, dict(type="meetings", meetings=[meeting()]))
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_a_stopped_call_that_ended_while_connected_is_new_after_a_reconnect(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    send_packet(stream, dict(type="meetings", meetings=[]))
    wait_for(lambda: not request(path=path)["meetings"])
    connection, stream = reconnect(path, connection, stream)
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_a_quiet_stopped_call_stays_stopped_while_its_link_is_up(service, monkeypatch):
    from cassini_viewer import browser_service

    _, path = service
    monkeypatch.setattr(browser_service, "STOPPED_CALL_GRACE", 0.3)
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    time.sleep(0.5)  # Longer than the grace, with no packet for this call.
    # Another browser's inventory runs the expiry; then this link reconnects.
    other, other_stream = browser(path, "5151:12", meetings=[meeting(7)])
    try:
        assert read_packet(other_stream)["tabId"] == 7
        connection, stream = reconnect(path, connection, stream)
        assert no_command(connection, stream), "the stopped call was started again"
    finally:
        for closable in (stream, connection, other_stream, other):
            closable.close()


def test_other_profile_and_restarted_browser_do_not_inherit_a_stopped_call(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    # Another profile of the same browser process has its own tabs.
    connection, stream = reconnect(path, connection, stream, meetings=[meeting(7)])
    command = read_packet(stream)
    assert command["action"] == "start" and command["tabId"] == 7
    # After a browser restart the process, and so the instance, is new.
    connection, stream = reconnect(path, connection, stream, instance="5151:12")
    try:
        assert read_packet(stream)["tabId"] == 3
    finally:
        stream.close()
        connection.close()


def test_profiles_sharing_a_browser_instance_keep_their_own_calls(service):
    _, path = service
    first, first_stream = browser(path, "4242:99")
    second, second_stream = browser(path, "4242:99", meetings=[meeting(7)])
    request("configure", path=path, autoRecord=True)
    for stream in (first_stream, second_stream):
        command = read_packet(stream)
        send_packet(
            stream,
            dict(
                type="result",
                requestId=command["requestId"],
                ok=True,
                status=dict(recording=True, tabId=command["tabId"]),
            ),
        )

    def recording_ids():
        ids = {m["tabId"]: m["id"] for m in request(path=path)["meetings"] if m["recording"]}
        return ids if len(ids) == 2 else None

    ids = wait_for(recording_ids)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(request, "stop", path=path, id=ids[3])
        command = read_packet(first_stream)
        send_packet(
            first_stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False))
        )
        future.result(2)
    # The first profile reconnects while the second one stays connected.
    first_stream.close()
    first.close()
    wait_for(lambda: request(path=path)["browsers"] == 1)
    first, first_stream = browser(path, "4242:99")
    try:
        assert no_command(first, first_stream), "the stopped call was started again"
        assert no_command(second, second_stream)
    finally:
        for stream, connection in ((first_stream, first), (second_stream, second)):
            stream.close()
            connection.close()


def test_enabling_automatic_recording_while_disconnected_rearms_the_call(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    request("configure", path=path, autoRecord=False)
    stream.close()
    connection.close()
    wait_for(lambda: request(path=path)["browsers"] == 0)
    request("configure", path=path, autoRecord=True)
    connection, stream = browser(path, "4242:99")
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_another_meeting_in_the_stopped_call_tab_is_new(service):
    _, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    # The call ended while disconnected and the tab joined another meeting.
    connection, stream = reconnect(
        path, connection, stream, meetings=[dict(meeting(), url="https://meeting.example/next")]
    )
    try:
        assert read_packet(stream)["action"] == "start"
    finally:
        stream.close()
        connection.close()


def test_command_failure_is_visible_and_no_retry_loop(service):
    server, path = service
    connection, stream = browser(path)
    try:
        request("configure", path=path, autoRecord=True)
        command = read_packet(stream)
        send_packet(stream, dict(type="result", requestId=command["requestId"], ok=False, error="No accessible audio"))
        wait_for(lambda: request(path=path)["meetings"][0]["error"])
        assert request(path=path)["meetings"][0]["error"] == "No accessible audio"
        server.policy()
        assert len(next(iter(server.browsers.values())).attempted) == 1
        directory = wait_for(lambda: next((c.directory for c in server.captures.values() if c.directory), None))
        assert directory.parent == server.capture_parent
    finally:
        stream.close()
        connection.close()


def test_missing_browser_and_untrusted_inventory(service):
    _, path = service
    with pytest.raises(ValueError, match="no longer connected"):
        request("start", path=path, id="missing:1")
    connection, stream = browser(path)
    try:
        send_packet(
            stream,
            dict(type="meetings", meetings=[dict(meeting(), sourceCount=True), dict(meeting(4), url="file:///tmp/x")]),
        )
        wait_for(lambda: not request(path=path)["meetings"])
    finally:
        stream.close()
        connection.close()


def test_reconnect_waits_for_status_before_auto_record(service):
    server, path = service
    request("configure", path=path, autoRecord=True)
    connection = connect(path)
    stream = connection.makefile("rwb")
    try:
        send_packet(stream, dict(role="extension"))
        send_packet(stream, dict(type="meetings", meetings=[meeting()]))
        wait_for(lambda: request(path=path)["meetings"])
        server.policy()
        state = next(iter(server.browsers.values()))
        assert not state.scheduled and not state.attempted
        send_packet(stream, dict(type="status", recording=True, tabId=3))
        wait_for(lambda: request(path=path)["meetings"][0]["recording"])
        server.policy()
        assert not state.pending, "already running capture must not be started again"
    finally:
        stream.close()
        connection.close()


def test_control_host_relays_commands_over_native_framing(service):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from cassini_viewer.recording_session import read_message, write_message

    _, path = service
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys;from cassini_viewer.browser_control_host import main;sys.exit(main(path=sys.argv[1]))",
            str(path),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for packet in [
            dict(type="hello", version=1),
            dict(type="status", recording=False),
            dict(type="meetings", meetings=[meeting()]),
        ]:
            write_message(process.stdin, packet)
        listing = wait_for(lambda: s if (s := request(path=path))["meetings"] else None)
        with ThreadPoolExecutor(1) as executor:
            result = executor.submit(request, "start", path=path, id=listing["meetings"][0]["id"])
            command = read_message(process.stdout)
            assert command["type"] == "command" and command["tabId"] == 3
            write_message(
                process.stdin,
                dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3)),
            )
            assert result.result(2)["meetings"][0]["recording"]
        process.stdin.close()
        assert process.wait(3) == 0, process.stderr.read().decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_control_host_survives_an_idle_broker(service, monkeypatch):
    import os

    from cassini_viewer import browser_control_host
    from cassini_viewer.recording_session import read_message, write_message

    _, path = service
    # Shorten the connect timeout so a link that kept it would fail within the test.
    monkeypatch.setattr(browser_control_host, "connect", lambda target: connect(target, timeout=0.2))
    incoming_read, incoming_write = os.pipe()
    outgoing_read, outgoing_write = os.pipe()
    with (
        open(incoming_read, "rb", buffering=0) as incoming,
        open(incoming_write, "wb", buffering=0) as to_host,
        open(outgoing_read, "rb", buffering=0) as from_host,
        open(outgoing_write, "wb", buffering=0) as outgoing,
    ):
        executor = ThreadPoolExecutor(1)
        host = executor.submit(browser_control_host.main, input_stream=incoming, output_stream=outgoing, path=path)
        try:
            for packet in [
                dict(type="hello", version=1),
                dict(type="status", recording=False),
                dict(type="meetings", meetings=[meeting()]),
            ]:
                write_message(to_host, packet)
            listing = wait_for(lambda: s if (s := request(path=path))["meetings"] else None)
            time.sleep(0.6)  # Three connect timeouts with nothing from the broker.
            # A dropped link leaves the broker without this browser and its meetings.
            assert request(path=path)["meetings"] == listing["meetings"], "the control link dropped while idle"
            with ThreadPoolExecutor(2) as commands:
                result = commands.submit(request, "start", path=path, id=listing["meetings"][0]["id"])
                command = commands.submit(read_message, from_host).result(2)
                write_message(
                    to_host,
                    dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=True, tabId=3)),
                )
                assert result.result(2)["meetings"][0]["recording"]
        finally:
            to_host.close()  # Chrome closing the port ends the host either way.
            executor.shutdown(wait=True)
            outgoing.close()
        assert host.result(3) == 0


def test_shared_control_host_vectors():
    # The runner validates against the JSON schemas; meson treats these as optional too.
    pytest.importorskip("jsonschema")
    pytest.importorskip("rfc3339_validator")
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runner = root / "browser-protocol/conformance/control/run.py"
    result = subprocess.run(
        [sys.executable, str(runner), str(root / "browser-extension/control_host.py")],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    count = len(json.loads(runner.with_name("vectors.json").read_text()))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{count} passed, 0 failed" in result.stdout


def test_stop_expiry_is_checked_before_reconnecting_inventory_can_refresh_it(service):
    from cassini_viewer.browser_service import STOPPED_CALL_GRACE

    server, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    stream.close()
    connection.close()
    wait_for(lambda: not server.browsers)
    key = ("4242:99", 3)
    with server.lock:
        url, seen = server.stopped_calls[key]
        server.stopped_calls[key] = (url, seen - STOPPED_CALL_GRACE - 1)
    connection, stream = browser(path, "4242:99")
    connection.settimeout(2)
    try:
        assert read_packet(stream)["action"] == "start"
        assert key not in server.stopped_calls
    finally:
        stream.close()
        connection.close()


def test_direct_navigation_to_another_call_clears_suppression_without_empty_inventory(service):
    server, path = service
    connection, stream = browser(path, "4242:99")
    request("configure", path=path, autoRecord=True)
    stop_automatic_call(path, stream)
    connection.settimeout(2)
    try:
        send_packet(stream, dict(type="meetings", meetings=[dict(meeting(), url="https://meeting.example/next")]))
        command = read_packet(stream)
        assert command["action"] == "start" and command["tabId"] == 3
        assert ("4242:99", 3) not in server.stopped_calls
    finally:
        stream.close()
        connection.close()
