# SPDX-License-Identifier: GPL-3.0-or-later
"""Application voice bookkeeping without initialising GTK or connecting devices."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_voice_store import RECORDING, VoiceStore, enrol

from cassini_viewer.library import LibrarySnapshot


@pytest.fixture(scope="module")
def application_class(tmp_path_factory):
    gi = pytest.importorskip("gi")
    try:
        for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
            gi.require_version(namespace, version)
    except ValueError:
        pytest.skip("Application bookkeeping also runs in GTK-enabled distro and Recording jobs")
    from gi.repository import Gio

    compiler = shutil.which("glib-compile-resources")
    if compiler is None:
        pytest.skip("The application resource compiler is unavailable")
    source = Path(__file__).parents[1] / "src"
    resource = tmp_path_factory.mktemp("voice-resources") / "cassini.gresource"
    subprocess.run(
        [
            compiler,
            str(source / "cassini-viewer.gresource.xml"),
            "--sourcedir=" + str(source),
            "--target=" + str(resource),
        ],
        check=True,
    )
    Gio.resources_register(Gio.Resource.load(str(resource)))
    from cassini_viewer.main import CassiniApplication

    return CassiniApplication


def test_unavailable_library_preserves_voice_meeting_lists_until_a_valid_scan(application_class, tmp_path):
    store = VoiceStore(tmp_path / "private")
    alice = enrol(store)
    store.sync_library([RECORDING])
    writes = []

    def submit(operation, keys):
        writes.append(keys)
        operation(keys)

    monitor = SimpleNamespace(_closed=False, _executor=SimpleNamespace(submit=submit))
    app = SimpleNamespace(
        _voice_library_keys=frozenset([RECORDING]),
        get_windows=lambda: [SimpleNamespace(monitor=monitor)],
        voice_store=store,
        settings_dialog=None,
        transcription_enabled=False,
    )
    before = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    application_class.observe_library(app, LibrarySnapshot(unavailable=True), None)
    assert not writes and app._voice_library_keys == frozenset([RECORDING])
    assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == before
    assert store.snapshot()["voices"] == [alice]
    available = LibrarySnapshot(meetings=(SimpleNamespace(directory=Path(RECORDING)),))
    application_class.observe_library(app, available, None)
    assert not writes and store.snapshot()["voices"] == [alice]
    # A successful empty scan really does mean the meeting left the Library.
    application_class.observe_library(app, LibrarySnapshot(), None)
    assert writes == [frozenset()] and store.snapshot()["voices"][0]["meetings"] == []
