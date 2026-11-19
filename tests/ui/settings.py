#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Preferences switches, persisted portal binding and browser broker controls."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Gio, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.browser_service import BrowserService, request, send_packet  # noqa: E402
from cassini_talk.browser_service import connect as broker_connect  # noqa: E402
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.player import Player  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402


def until(check):
    dispatch_until(check, "Settings GUI did not complete")


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(tmp / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(tmp / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    connect = LibraryWindow._connect_player

    def private_connect(window):
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        if isinstance(window.player, FolderPlayer):
            window.player.audio_sink = sink
        elif isinstance(window.player, Player):
            window.player._pipeline.set_property("audio-sink", sink)
        connect(window)

    LibraryWindow._connect_player = private_connect
    service = BrowserService(tmp / "browser-config.json")
    ready = threading.Event()
    path = tmp / "broker/browser.sock"
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    desktop = DesktopServices()
    app = CassiniApplication("com.gocassini.SettingsTest", "test", "development")
    # Exercise real startup without the screenshot/audio opt-out. Everything,
    # including the desktop portal service and browser profiles, is private.
    with patch.dict(os.environ):
        os.environ.pop("CASSINI_SCREENSHOT_ISOLATED", None)
        os.environ.pop("CASSINI_ISOLATED_AUDIO", None)
        app.register(None)
    assert app.global_shortcut is None, "Fresh startup must not ask for global shortcuts"
    assert not desktop.calls
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    from cassini_talk.library import library_root
    from cassini_talk.settings_dialog import SettingsDialog, path_caption

    dialog = SettingsDialog(app, service_path=path)
    dialog.present(window)
    until(lambda: dialog._future is None and window.get_width() >= 1000)
    assert len(dialog.pages) == 4
    from gi.repository import Adw

    from cassini_talk import i18n
    from cassini_talk.strings import plural
    from cassini_talk.strings import text as ux_text

    def switcher_tab_labels(root):
        found = []
        stack = [root]
        while stack:
            widget = stack.pop()
            if (
                isinstance(widget, Gtk.Label)
                and widget.get_width() > 0
                and widget.get_ancestor(Adw.ViewSwitcher) is not None
                and widget.get_ancestor(Adw.HeaderBar) is not None
            ):
                found.append(widget)
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found

    until(lambda: len(switcher_tab_labels(dialog)) == 4)
    for lang in i18n.LANGUAGES:
        with i18n.using_language(lang):
            for page_name, page in dialog.pages.items():
                page.set_title(ux_text("settings.tab." + page_name))
        frames = []
        window.add_tick_callback(lambda *_, seen=frames: (seen.append(1), len(seen) < 3)[1])
        window.queue_draw()
        until(lambda seen=frames: len(seen) >= 2)
        tab_labels = switcher_tab_labels(dialog)
        assert len(tab_labels) == 4
        assert not any(lbl.get_layout().is_ellipsized() for lbl in tab_labels), (
            f"Preferences tab truncated in {lang}: {[(lbl.get_text(), lbl.get_width()) for lbl in tab_labels]}"
        )
    for page_name, page in dialog.pages.items():
        page.set_title(ux_text("settings.tab." + page_name))
    until(lambda: dialog.pages["browserCompanion"].get_title() == "Browser companion")
    assert dialog.get_title() == "Preferences" and not dialog.get_search_enabled()
    # L-58 (D103 §1, D107 §1): one map; a heading (and description) above each card.
    shown = labels(dialog)
    for key in (
        "settings.recording.group.starting",
        "settings.recording.firstRecord.subtitle",
        "settings.recording.group.browser",
        "test.action",
        "settings.recording.test.descriptionShort",
        "settings.transcription.speech.heading",
        "models.keepTranscript",
        "models.builtIn",
        "settings.transcription.footnote",
        "settings.library.folder.title",
        "settings.library.folder.descriptionShort",
        "settings.library.deleted.caption",
        "settings.library.group.keepingCopy",
        "settings.library.zip.title",
        "settings.library.zip.subtitle",
        "settings.library.backUp.caption",
        "settings.browser.heading",
        "settings.browser.body",
        "companion.worksWith",
        "companion.privacy",
    ):
        assert ux_text(key) in shown, key
    # Replaced by D107: the line above Transcription, the backup row, "Deleted meetings", the Trash help
    # text (Help keeps it) and the "Without it" section.
    for key in (
        "settings.transcription.network.heading",
        "settings.transcription.privacyLine",
        "settings.recording.test.description",
        "settings.library.backUp.title",
        "settings.library.group.deleted",
        "settings.library.deleted.title",
        "help.trash.body",
        "companion.bodyShort",
        "companion.without.heading",
        "companion.without.body",
        "export.zip.all",
    ):
        assert ux_text(key) not in shown, key
    # Every page's sections sit 26 px apart (D107 §1).
    for page in dialog.pages.values():
        groups = []
        stack = [page]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Adw.PreferencesGroup) and widget.get_visible() and widget.get_height() > 0:
                groups.append(widget)
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        if page.get_mapped() and len(groups) > 1:
            tops = sorted((g.compute_bounds(page)[1].get_y(), g.compute_bounds(page)[1].get_height()) for g in groups)
            gaps = {round(b[0] - (a[0] + a[1])) for a, b in zip(tops, tops[1:], strict=False)}
            assert gaps <= {26, 36}, (page.get_name(), gaps)  # The footnote adds 10 px above itself.
    assert dialog.test_button.get_label() == "Test"
    # D107 §1: never set up, so "Needs the browser companion · Set it up", the switch greyed and a red dot.
    assert dialog.browser_meetings_status.get_label() == "Needs the browser companion ·"
    assert dialog.browser_set_up.get_visible() and dialog.browser_set_up.get_label() == "Set it up"
    assert not dialog.browser_meetings_dot.get_visible() and not dialog.automatic.get_sensitive()
    # The card's title names this container's state (no browser here); not connected, so a red dot.
    assert dialog.browser_dot.has_css_class("status-dot-off"), dialog.browser.get_title()
    assert not dialog.browser_remove.get_visible()
    dialog.browser_set_up.emit("clicked")
    assert dialog.get_visible_page_name() == "browserCompanion"
    dialog.set_visible_page_name("recording")
    # "9 meetings · 263 MB": sized off the GTK thread, then shown under the folder path.
    until(lambda: dialog.library_stats is not None)
    meetings = len(window.snapshot.meetings)
    assert dialog.library_stats.startswith(f"{meetings} meetings · "), dialog.library_stats
    # D107 §1: the card is "Music › Cassini" (here the fixture folder) over its meetings and size.
    assert dialog.folder.get_title() == path_caption(library_root())
    assert dialog.folder.get_subtitle() == dialog.library_stats
    assert dialog.zip_button.get_sensitive() and not dialog.move_lines.get_visible()
    # Export… from Preferences works while the sidebar search is open, and its toasts land on the
    # Preferences dialog (the window's own overlay is behind it, under the modal scrim).
    app.settings_dialog = dialog
    preference_toasts = []
    dialog.add_toast = preference_toasts.append
    window._enter_search(focus=False)
    assert window.search_page is not None
    window._refresh_export()
    dialog.refresh_library()
    assert dialog.zip_button.get_sensitive(), "Search must not grey out Export all as ZIP"
    dialog.zip_button.emit("clicked")
    until(lambda: window.zip_export_dialog is not None and window.zip_export_dialog.get_mapped())
    zdialog = window.zip_export_dialog
    assert zdialog.get_child() is zdialog.toasts, "Export errors need an overlay inside the ZIP dialog"
    for check in zdialog.checks.values():
        check.set_active(False)
    first = zdialog.choices[0]
    zdialog.checks[first.key].set_active(True)
    zips = tmp / "zips"
    zips.mkdir()
    # Where the toast goes is under test here; batch-import.py drives the real export.
    from cassini_talk import zip_export_dialog

    with patch.object(zip_export_dialog, "export_meetings_zip", lambda targets, folder, **_: folder / "Cassini.zip"):
        zdialog._start_export(zips, all_items=False)
        until(lambda: not zdialog.busy)
    assert zdialog.closed and zdialog.output is not None, (zdialog.closed, zdialog.output, zdialog.future)
    assert preference_toasts == [zdialog.success_toast]
    assert zdialog.success_toast.get_title() == plural("export.zip.done", 1)
    window.visible_toast("Shown on Preferences")
    assert preference_toasts[-1].get_title() == "Shown on Preferences"
    window._close_search()
    del dialog.add_toast
    assert dialog.explain.get_active()
    dialog.explain.set_active(False)
    assert app.recording_settings.get_boolean("skip-record-first")
    dialog.explain.set_active(True)
    assert not app.recording_settings.get_boolean("skip-record-first")
    assert dialog.shortcut.get_subtitle() == "Ctrl+Alt+R"
    assert not app.recording_settings.get_boolean("record-shortcut-enabled")
    assert dialog.change_shortcut.get_sensitive() and not desktop.calls
    # Declining must leave opt-in false and allow another explicit attempt.
    desktop.denied = True
    dialog.change_shortcut.emit("clicked")
    until(lambda: app.global_shortcut and app.global_shortcut.error)
    assert not app.recording_settings.get_boolean("record-shortcut-enabled")
    dialog.refresh()
    assert dialog.change_shortcut.get_sensitive()
    desktop.denied = False
    dialog.change_shortcut.emit("clicked")
    pending = app.global_shortcut
    dialog.change_shortcut.emit("clicked")
    assert app.global_shortcut is pending, "Repeated clicks must not create overlapping sessions"
    until(lambda: app.global_shortcut.bound)
    assert app.recording_settings.get_boolean("record-shortcut-enabled")
    assert [c["method"] for c in desktop.calls] == ["CreateSession", "CreateSession", "ListShortcuts", "BindShortcuts"]
    desktop.shortcut_description = "Ctrl+Shift+R"
    dialog.change_shortcut.emit("clicked")
    until(lambda: app.global_shortcut.trigger_description == "Ctrl+Shift+R")
    dialog.refresh()
    assert dialog.shortcut.get_subtitle() == "Ctrl+Shift+R"
    assert app.recording_settings.get_string("record-shortcut-description") == "Ctrl+Shift+R"
    assert any(c["method"] == "ConfigureShortcuts" for c in desktop.calls)
    stale = request(path=path)
    dialog.automatic.set_active(True)
    app._browser_changed(stale)
    assert app._auto_recording_held, "An old poll undid an accepted auto-record choice"
    until(lambda: service.auto_record and dialog._future is None and app._auto_recording_pending is None)
    app._browser_changed(stale)
    assert app._auto_recording_held, "An old poll undid the completed preference write"
    assert json.loads((tmp / "browser-config.json").read_text())["autoRecord"] is True
    with broker_connect(path) as browser:
        stream = browser.makefile("rwb")
        send_packet(stream, dict(role="extension", browserName="Chromium"))
        send_packet(stream, dict(type="hello", version=1))
        send_packet(stream, dict(type="status", recording=False))
        until(lambda: request(path=path)["connectedBrowsers"] == 1)
        dialog.refresh()
        until(lambda: dialog.browser.get_title() == "Connected to Chromium")
        # D107 §1: a green dot and the browser, right of "Browser meetings" too; the switch is available.
        assert dialog.browser_dot.has_css_class("status-dot-connected")
        assert dialog.browser_meetings_status.get_label() == "Connected to Chromium"
        assert dialog.browser_meetings_dot.get_visible() and not dialog.browser_set_up.get_visible()
        assert dialog.automatic.get_sensitive()
        assert dialog.browser_remove.get_visible() and dialog.browser_remove.get_label() == "Remove…"
        assert not dialog.browser_add.get_visible() and not dialog.browser_add_local.get_visible()
        # Remove… opens the Extensions page (never really launched here) and shows the one-line how-to.
        from cassini_talk import companion as companion_module

        opened = []
        with (
            patch.object(companion_module, "open_new_tab", lambda browser: opened.append(browser)),
            patch.object(companion_module, "check_browser_launch", lambda _process: None),
        ):
            dialog.browser_remove.emit("clicked")
            until(lambda: not dialog._removing)
        assert dialog.remove_help.get_visible()
        assert "On the Extensions page, find Cassini and click Remove." in labels(dialog)
        assert "Parakeet Ultra" not in labels(dialog) and "Tell speakers apart" not in labels(dialog)
        assert "In use for new meetings" in labels(dialog)
        assert "To check for updates. No meeting data is sent." not in labels(dialog)
        stream.close()
    with broker_connect(path) as unknown:
        stream = unknown.makefile("rwb")
        send_packet(stream, dict(role="extension"))
        send_packet(stream, dict(type="hello", version=1))
        send_packet(stream, dict(type="status", recording=False))
        until(lambda: request(path=path)["connectedBrowsers"] == 1)
        dialog.refresh()
        until(lambda: dialog.browser.get_title() == "Connected")
        stream.close()
    dialog.force_close()
    until(lambda: dialog.closed)
    # The second dialog is closed before its initial status delivery. Its
    # explicit choice must still be written after the observation is cancelled.
    second = SettingsDialog(app, service_path=path)
    second.present(window)
    until(lambda: second._future is None)
    assert second.automatic.get_active()
    second.refresh()
    second.automatic.set_active(False)
    second.force_close()
    until(lambda: second.closed and not service.auto_record and app._auto_recording_pending is None)
    assert not app._auto_recording_held
    assert json.loads((tmp / "browser-config.json").read_text())["autoRecord"] is False
    app.global_shortcut.close()
    restored = CassiniApplication("com.gocassini.SettingsRestoreTest", "test", "development")
    with patch.dict(os.environ):
        os.environ.pop("CASSINI_SCREENSHOT_ISOLATED", None)
        os.environ.pop("CASSINI_ISOLATED_AUDIO", None)
        restored.register(None)
    until(lambda: restored.global_shortcut and restored.global_shortcut.bound)
    assert restored.global_shortcut.trigger_description == "Ctrl+Shift+R"
    binding = [c for c in desktop.calls if c["method"] == "BindShortcuts"][-1]
    assert "preferred_trigger" not in binding["values"][1][0][1]
    restored.do_shutdown()
    desktop.close()
    window.close()
    app.do_shutdown()
    service.closed.set()
    thread.join(2)
    assert not thread.is_alive()
    print(
        "PASS Preferences: four pages, first-record binding, portal configure/cache, broker auto-record/status and immediate close"
    )
