# SPDX-License-Identifier: GPL-3.0-or-later
"""D44 capture-site time is persisted before publishing, independent of later TZ."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from cassini_talk.recording_session import ProtocolError, RecordingSession


@pytest.mark.parametrize("capture_kind", ["native", "browser"])
@pytest.mark.parametrize(
    "zone,start,offset,prefix",
    [
        ("Europe/Rome", "2026-10-25T00:59:00+00:00", 120, "2026-10-25--02.59-"),
        ("Europe/Rome", "2026-10-25T01:01:00+00:00", 60, "2026-10-25--02.01-"),
        ("Asia/Kathmandu", "2026-10-30T12:34:00+00:00", 345, "2026-10-30--18.19-"),
        ("Pacific/Kiritimati", "2026-10-30T12:34:00+00:00", 840, "2026-10-31--02.34-"),
    ],
)
def test_first_checkpoint_has_site_offset_and_later_zone_cannot_change_it(
    tmp_path, zone, start, offset, prefix, capture_kind
):
    # TZ changes live only in this private child, never the test runner/desktop.
    code = """
import json, os, sys, time
from datetime import UTC, datetime
from pathlib import Path
from cassini_talk.recording_session import RecordingSession
from cassini_talk.library_v2.finalize import start_of
time.tzset()
instant = datetime.fromisoformat(sys.argv[2])
if sys.argv[3] == "browser":
    from cassini_talk.browser_capture import BrowserCapture
    capture = BrowserCapture(lambda *_: None, parent=sys.argv[1])
    capture.start(dict(type="start", captureId="site-offset", timelineOriginMs=int(instant.timestamp()*1000), microphone=False))
    session = capture.host.session
else:
    session = RecordingSession(sys.argv[1], started_at=instant)
first = json.loads((session.directory / "session.json").read_text())
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()
session.checkpoint()
saved = json.loads((session.directory / "session.json").read_text())
prefix, source, date, _ = start_of(saved, {}, "America/Los_Angeles", datetime.now(UTC))
print(json.dumps(dict(first=first, saved=saved, prefix=prefix, source=source, date=date)))
"""
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), start, capture_kind],
        env=dict(os.environ, TZ=zone, PYTHONPATH=str(source)),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    value = json.loads(result.stdout)
    assert value["first"]["startedAtUtcOffsetMinutes"] == offset
    assert value["first"] == value["saved"]
    assert value["prefix"] == prefix
    assert value["source"] == "recorded"


def test_naive_start_is_rejected_before_creating_any_recording_folder(tmp_path):
    with pytest.raises(ProtocolError, match="absolute time"):
        RecordingSession(tmp_path, started_at=datetime(2026, 10, 30))
    assert not list(tmp_path.iterdir())


def test_parent_directory_durability_is_scoped_to_managed_sessions(tmp_path, monkeypatch):
    from cassini_talk.library_v2 import fsutil

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    calls = []
    original = fsutil.fsync_dir

    def synced(path):
        calls.append(path)
        original(path)

    monkeypatch.setattr(fsutil, "fsync_dir", synced)
    legacy = RecordingSession(tmp_path / "explicit-raw-fixture")
    legacy.finish()
    assert not calls  # Existing raw protocol geometry/checkpoints stay unchanged.
    managed = RecordingSession(None)
    managed.finish()
    assert managed.directory.parent in calls
