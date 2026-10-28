# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent writer ownership and per-session microphone choice, without devices."""

import base64
import json
import struct
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
from cassini_viewer.browser_service import BrowserService, request


class Tap:
    instances = []

    def __init__(self, kind, origin, append, failed, *, level):
        self.kind, self.append = kind, append
        self.started = self.stopped = False
        self.instances.append(self)

    def start(self):
        self.started = True
        self.append(struct.pack("<hhhh", 100, 200, 300, 400), 100)

    def stop(self):
        self.stopped = True


@pytest.fixture
def server(tmp_path):
    Tap.instances = []
    service = BrowserService(tmp_path / "config.json", capture_parent=tmp_path / "library", tap_factory=Tap)
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield service, path, tmp_path / "library"
    service.closed.set()
    worker.join(2)
    assert not worker.is_alive()


def start(client, identifier, *, microphone=True):
    return client.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId=identifier,
            timelineOriginMs=int(time.time() * 1000),
            microphone=microphone,
            title=identifier,
        )
    )


def remote(client, identifier):
    client.handle(dict(type="track", id=identifier, label="Other", kind="remote", sampleRate=48000, channels=1))
    client.handle(
        dict(type="audio", id=identifier, sequence=0, offsetMs=200, pcm=base64.b64encode(b"\x01\x00" * 4).decode())
    )


def test_independent_native_host_writers_copy_only_the_selected_microphone(server):
    service, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        first = start(one, "first")
        second = start(two, "second", microphone=False)
        assert first["path"] != second["path"]
        assert len(Tap.instances) == 1 and Tap.instances[0].started
        remote(one, "remote-one")
        remote(two, "remote-two")
        assert len([s for s in request(path=path)["statuses"] if s["recording"]]) == 2
        one.handle(dict(type="stop"))
        states = request(path=path)["statuses"]
        assert [s["captureId"] for s in states if s["recording"]] == ["second"]
        assert Tap.instances[0].stopped
        two.handle(
            dict(
                type="audio",
                id="remote-two",
                sequence=1,
                offsetMs=200 + 4 * 1000 / 48000,
                pcm=base64.b64encode(b"\x02\x00" * 4).decode(),
            )
        )
        two.handle(dict(type="stop"))
        manifests = [json.loads((p / "session.json").read_text()) for p in sorted(root.iterdir())]
        assert len(manifests) == 2 and all(m["state"] == "completed" for m in manifests)
        first = next(m for m in manifests if m["title"] == "first")
        second = next(m for m in manifests if m["title"] == "second")
        assert [t["kind"] for t in first["tracks"]] == ["microphone", "remote"]
        assert [t["kind"] for t in second["tracks"]] == ["remote"]
        assert first["tracks"][0]["labelSource"] == "device"
        assert first["tracks"][0]["startOffsetMs"] == 100
        assert second["tracks"][0]["frames"] == 8
        for folder in root.iterdir():
            metadata = json.loads((folder / "session.json").read_text())
            for track in metadata["tracks"]:
                with wave.open(str(folder / track["file"])) as audio:
                    assert audio.getnframes() == track["frames"]
        assert len(service.recordings) == 2
    finally:
        one.close()
        two.close()


def test_audio_peer_eof_falls_back_only_its_own_capture_and_keeps_the_microphone(server):
    service, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        first = start(one, "first")
        start(two, "second", microphone=False)
        remote(one, "remote-one")
        remote(two, "remote-two")
        one.close()
        end = time.monotonic() + 2
        while time.monotonic() < end and not service.captures["first"].browser_lost:
            time.sleep(0.01)
        assert [s["captureId"] for s in request(path=path)["statuses"] if s["recording"]] == ["first", "second"]
        from pathlib import Path

        value = json.loads((Path(first["path"]) / "session.json").read_text())
        assert value["state"] == "recording"
        assert value["events"][-1] == dict(
            atMs=value["events"][-1]["atMs"], type="othersSource", source="system", reason="lost"
        )
        assert value["tracks"][-1]["kind"] == "system"
        assert value["tracks"][-1]["startOffsetMs"] == value["events"][-1]["atMs"]
        assert not service.captures["first"].microphone.stopped
        assert not service.captures["second"].browser_lost
        service.captures["first"].stop()
        two.handle(dict(type="stop"))
    finally:
        one.close()
        two.close()


def test_duplicate_identity_is_rejected_without_an_orphan_folder(server):
    _, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        start(one, "same", microphone=False)
        with pytest.raises(ValueError, match="identity already exists"):
            start(two, "same", microphone=False)
        assert len(list(root.iterdir())) == 1
        one.handle(dict(type="stop"))
    finally:
        one.close()
        two.close()


def test_two_stop_requests_join_one_native_finalization(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "one")
        capture = service.captures["one"]
        entered, release = threading.Event(), threading.Event()
        original = capture.microphone.stop

        def stop():
            entered.set()
            assert release.wait(2)
            original()

        capture.microphone.stop = stop
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(capture.stop)
            assert entered.wait(1)
            second = pool.submit(capture.stop)
            release.set()
            assert first.result(2)["path"] == second.result(2)["path"]
        assert capture.finished
    finally:
        client.close()
