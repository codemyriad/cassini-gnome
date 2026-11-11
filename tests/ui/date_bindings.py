# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronous real Inspector rows for GLib/POSIX and zone-file time zones."""

import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import Adw, Gio, Gtk

from cassini_talk import portable
from cassini_talk.i18n import using_language
from cassini_talk.window import CassiniWindow


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def check_recording_details_time():
    fixture = Path(__file__).resolve().parents[1] / "conformance/opus/027-recorded-at-utc.opus"
    original = portable.read_meeting(str(fixture))
    with tempfile.TemporaryDirectory() as temporary:
        alias = Path(temporary) / "localtime"
        shutil.copyfile("/usr/share/zoneinfo/Asia/Tokyo", alias)
        for zone in ("JST-9", ":" + str(alias)):
            for utc, local, expected in (
                ("2026-11-09T23:31:48Z", None, "Tuesday 10 November 2026 08:31"),
                (None, "2026-11-09T23:31:48", "Monday 9 November 2026 23:31"),
                (None, None, None),
            ):
                document = deepcopy(original)
                document.manifest["meeting"].pop("recordedAtUtc", None)
                document.manifest["meeting"].pop("recordedAtLocal", None)
                document.tags["DATE"] = document.created_at
                if utc:
                    document.manifest["meeting"]["recordedAtUtc"] = utc
                if local:
                    document.manifest["meeting"]["recordedAtLocal"] = local
                box = Gtk.Box()
                controller = SimpleNamespace(
                    properties_box=box,
                    meeting=document,
                    player=SimpleNamespace(duration=0),
                    file=Gio.File.new_for_path(str(fixture)),
                    transcript_id=None,
                    _trust_card=lambda: Gtk.Box(),
                )
                with patch.dict(os.environ, TZ=zone), using_language("en"):
                    CassiniWindow._populate_properties(controller)
                rows = [w for w in descendants(box) if isinstance(w, Adw.ActionRow) and w.get_title() == "Recorded"]
                if expected is None:
                    assert not rows
                else:
                    assert len(rows) == 1 and rows[0].get_subtitle() == expected, (
                        zone,
                        [r.get_subtitle() for r in rows],
                    )
    print("PASS: real Inspector Recorded rows retain resolved GLib wall-clock fields")
