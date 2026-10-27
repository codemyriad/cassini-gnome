# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassini_viewer.browser_service import BrowserService, connect, read_packet, request, send_packet


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "private/browser.sock"
    notifications = []

    def notify(title, stop):
        notifications.append((title, stop))
        return lambda: None

    service = BrowserService(tmp_path / "settings.json", command_timeout=0.3, notifier=notify)
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


def browser(path):
    connection = connect(path)
    stream = connection.makefile("rwb")
    send_packet(stream, dict(role="extension"))
    send_packet(stream, dict(type="hello", version=1))
    send_packet(stream, dict(type="status", recording=False, tabId=None))
    send_packet(stream, dict(type="meetings", meetings=[meeting()]))
    wait_for(lambda: request(path=path)["meetings"])
    return connection, stream


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


def test_shared_control_host_vectors():
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
