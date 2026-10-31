#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real captured-monitor mute facts on private Pulse, including browser fallback."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh; no desktop audio is allowed")
if not os.environ.get("PULSE_SERVER", "").startswith("unix:/tmp/cassini-audio-"):
    raise SystemExit("A private Pulse socket is required")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cassini_talk.browser_capture import BrowserCapture  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402
from cassini_talk.output_mute import intervals  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ["CASSINI_TEST_ARTIFACTS"])
errors = []


def until(condition, description):
    loop = GLib.MainLoop()
    failure = []

    def check():
        try:
            assert not errors, errors
            done = condition()
        except BaseException as error:
            failure.append(error)
            done = True
        if done:
            loop.quit()
        return not done

    def expired():
        failure.append(AssertionError(f"Timed out waiting for {description}; errors={errors}"))
        loop.quit()
        return False

    tick = GLib.timeout_add(10, check)
    limit = GLib.timeout_add_seconds(12, expired)
    loop.run()
    if GLib.MainContext.default().find_source_by_id(tick):
        GLib.source_remove(tick)
    if GLib.MainContext.default().find_source_by_id(limit):
        GLib.source_remove(limit)
    if failure:
        raise failure[0]


def pulse(*args):
    return subprocess.run(["pactl", *args], check=True, capture_output=True, text=True, timeout=3).stdout.strip()


def facts(capture):
    directory = capture.session.directory if isinstance(capture, NativeRecorder) else capture.directory
    return intervals(read_metadata(directory))


native = NativeRecorder()
native.connect("error", lambda _recorder, message: errors.append(message))
browser = None
try:
    pulse("set-sink-mute", "cassini_desktop", "1")
    native.start(artifacts)
    until(lambda: native.pipeline is not None and native.output_muted, "muted-at-start native warning")
    assert facts(native)[0]["trackId"] == "system"
    # A new default is not necessarily the output already captured. Pin the
    # existing recording stream to its old monitor and mute only that output.
    pulse("set-default-sink", "cassini_microphone")
    sources = json.loads(pulse("--format=json", "list", "source-outputs"))
    system_source = pulse("list", "short", "sources")
    source_index = next(line.split()[0] for line in system_source.splitlines() if "cassini_desktop.monitor" in line)
    for row in sources:
        if str(row["source"]) == source_index:
            pulse("move-source-output", str(row["index"]), "cassini_desktop.monitor")
    pulse("set-sink-mute", "cassini_microphone", "0")
    native._mute_observer.refresh()
    until(lambda: native._output_sample()[1] == "cassini_desktop.monitor", "actual non-default monitor identity")
    assert native.output_muted, "An unrelated default output cleared the warning"
    pulse("set-sink-mute", "cassini_desktop", "0")
    native._mute_observer.refresh()
    until(lambda: not native.output_muted and "endMs" in facts(native)[0], "native unmute interval end")
    pulse("set-sink-mute", "cassini_desktop", "1")
    native._mute_observer.refresh()
    until(lambda: native.output_muted and len(facts(native)) == 2, "native mute while recording")
    choice = native.select(True, False)
    until(lambda: choice.done() and not native._changing_sources, "system source disabled")
    choice.result()
    until(lambda: not native.output_muted and "endMs" in facts(native)[-1], "disabled system span closes its fact")
    native.stop()
    until(lambda: native.pipeline is None and not native._finishing, "native Stop completes")
    pulse("set-default-sink", "cassini_desktop")
    browser = BrowserCapture(lambda *_: None, parent=artifacts)
    browser.start(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId="mute-fallback",
            timelineOriginMs=int(time.time() * 1000),
            microphone=False,
            title="Fallback",
        )
    )
    # A browser remote track does not depend on the muted sink.
    assert not browser.output_muted and not facts(browser)
    browser.fallback()
    until(lambda: browser.output_muted, "browser system fallback warning")
    system_id = browser.native_taps["system"][0]
    assert facts(browser)[0]["trackId"] == system_id
    browser.stop()
    assert not browser.output_muted and "endMs" in facts(browser)[0], "Stop left a live mute interval"
    (artifacts / "output-muted.json").write_text(
        json.dumps(dict(native=facts(native), browser=facts(browser)), indent=2)
    )
    print("PASS: muted start, observed monitor, unmute, mute, source off, browser-only, fallback, Stop")
finally:
    if native.pipeline is not None and not native.stopping:
        native.stop()
        until(lambda: native.pipeline is None and not native._finishing, "native cleanup")
    native._executor.shutdown(wait=True)
    if browser and not browser.finished:
        browser.stop()
