#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private real broker + native microphone + four paced browser PCM streams."""

import base64
import json
import math
import select
import sys
import threading
import time
from array import array
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from broker_fixture import stop_broker  # noqa: E402

from cassini_talk.browser_audio_proxy import BrowserAudioProxy  # noqa: E402
from cassini_talk.browser_service import BrowserService  # noqa: E402
from cassini_talk.native_tap import NativeTap  # noqa: E402


def report(value):
    print(json.dumps(value), flush=True)


def main():
    root, socket_path = map(Path, sys.argv[1:])
    service = BrowserService(
        root / "browser.json",
        capture_parent=root / "Library",
        tap_factory=partial(NativeTap, test=True),
        notifier=lambda *_: None,
    )
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(socket_path, ready), daemon=True)
    worker.start()
    assert ready.wait(5), "private broker socket was not ready"
    proxy = BrowserAudioProxy(path=socket_path)
    proxy.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId="spectrogram-four-voices",
            timelineOriginMs=int(time.time() * 1000),
            microphone=True,
            title="Spectrogram browser measurement",
        )
    )
    for n in range(4):
        proxy.handle(
            dict(type="track", id=f"voice-{n}", label=f"Person {n + 1}", kind="remote", sampleRate=48000, channels=1)
        )
    encoded = [
        base64.b64encode(
            array("h", (round(1800 * math.sin(2 * math.pi * hz * n / 48000)) for n in range(8192))).tobytes()
        ).decode()
        for hz in (300, 700, 1300, 2100)
    ]
    capture = service.captures["spectrogram-four-voices"]
    closed, errors = threading.Event(), []

    def send_audio():
        origin = time.monotonic()
        seq = 0
        try:
            while not closed.is_set():
                # Clock the actual browser chunk size; this is the signal
                # producer, not a test waiting for a presumed-ready service.
                if closed.wait(max(0, origin + seq * 8192 / 48000 - time.monotonic())):
                    break
                for n, pcm in enumerate(encoded):
                    if capture.browser_lost:
                        continue
                    proxy.handle(
                        dict(type="audio", id=f"voice-{n}", sequence=seq, offsetMs=seq * 8192 * 1000 / 48000, pcm=pcm)
                    )
                seq += 1
        except Exception as error:
            errors.append(str(error))

    producer = threading.Thread(target=send_audio, daemon=True)
    producer.start()
    report(dict(ready=True))
    before = None
    try:
        # The parent owns the process. A deadline detects a lost harness;
        # regular commands are event-driven through stdin, never a fixed sleep.
        while select.select([sys.stdin], [], [], 120)[0]:
            command = sys.stdin.readline().strip()
            if not command or command == "quit":
                break
            source = capture.spectrogram
            with source.lock:
                now = (
                    time.monotonic(),
                    time.process_time(),
                    source.analysis_ns,
                    source.calls,
                    source.analysed,
                    source.analysis_cpu_ns,
                )
                selected = {
                    side: sum(source.tracks[k].side == side for k in source.selected) for side in ("me", "others")
                }
            if command == "fallback":
                capture.fallback("lost")
                report(dict(events=capture.host.session.manifest["events"]))
            elif command == "begin":
                before = now
                report(dict(started=True))
            elif command == "report":
                elapsed = now[0] - before[0]
                report(
                    dict(
                        seconds=elapsed,
                        processCpuPercent=100 * (now[1] - before[1]) / elapsed,
                        analysisWallPercent=(now[2] - before[2]) / (elapsed * 1e7),
                        analysisCpuPercent=(now[5] - before[5]) / (elapsed * 1e7),
                        calls=now[3] - before[3],
                        analysed=now[4] - before[4],
                        selected=selected,
                        errors=errors,
                    )
                )
            else:
                raise ValueError("Unknown probe command")
    finally:
        closed.set()
        producer.join(5)
        assert not producer.is_alive(), "browser audio producer did not exit"
        proxy.handle(dict(type="stop"))
        proxy.close()
        stop_broker(service, worker, socket_path)


if __name__ == "__main__":
    main()
