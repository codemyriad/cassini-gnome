# SPDX-License-Identifier: GPL-3.0-or-later
"""D15 native Preferences. Broker IO runs off GTK; every visible label is shared copy."""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from gi.repository import Adw, Gio, GLib, Gtk

from . import browser_service
from .library import library_root
from .strings import plural, text

LOG = logging.getLogger(__name__)


def path_caption(path):
    path = Path(path)
    try:
        path = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return " › ".join(path.parts)


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, app, *, service_path=None):
        super().__init__(
            title=text("settings.title"),
            search_enabled=False,
            content_width=700,
            content_height=640,
            css_classes=["cassini-preferences"],
        )
        self.app = app
        self.service_path = service_path
        self.closed = False
        self.cancelled = Event()
        self._updating = False
        self._future = None
        self._queued = None
        self._snapshot = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-settings")
        self.settings = app.recording_settings
        self.pages = {}
        for name, icon in [
            ("recording", "audio-input-microphone-symbolic"),
            ("transcription", "view-list-symbolic"),
            ("library", "folder-symbolic"),
            ("browserCompanion", "web-browser-symbolic"),
        ]:
            page = Adw.PreferencesPage(name=name, title=text("settings.tab." + name), icon_name=icon)
            self.pages[name] = page
            self.add(page)
        self._recording()
        self._transcription()
        self._library()
        self._browser()
        self.set_visible_page_name("recording")
        self.connect("closed", self._closed)
        self.timer = GLib.timeout_add_seconds(1, self.refresh)
        self.refresh()

    def _recording(self):
        group = Adw.PreferencesGroup()
        self.pages["recording"].add(group)
        self.shortcut = Adw.ActionRow(title=text("settings.recording.shortcut.title"), use_markup=False)
        self.change_shortcut = Gtk.Button(label=text("settings.recording.changeShortcut"), valign=Gtk.Align.CENTER)
        self.change_shortcut.connect(
            "clicked", lambda *_: self.app.global_shortcut.configure() if self.app.global_shortcut else None
        )
        self.shortcut.add_suffix(self.change_shortcut)
        group.add(self.shortcut)
        self.explain = Adw.SwitchRow(title=text("settings.recording.firstRecord.title"), use_markup=False)
        self.settings.bind("skip-record-first", self.explain, "active", Gio.SettingsBindFlags.INVERT_BOOLEAN)
        group.add(self.explain)
        self.automatic = Adw.SwitchRow(
            title=text("settings.recording.autoRecord.title"),
            subtitle=text("settings.recording.autoRecord.subtitle"),
            use_markup=False,
        )
        self.automatic.connect("notify::active", self._automatic_changed)
        group.add(self.automatic)

    def _transcription(self):
        page = self.pages["transcription"]
        privacy = Adw.PreferencesGroup()
        line = Gtk.Box(spacing=12)
        line.append(Gtk.Image(icon_name="security-high-symbolic", css_classes=["success"], valign=Gtk.Align.START))
        line.append(Gtk.Label(label=text("settings.transcription.privacy"), wrap=True, xalign=0, hexpand=True))
        privacy.add(line)
        page.add(privacy)
        speech = Adw.PreferencesGroup(
            title=text("settings.transcription.speech.heading"),
            description=text("settings.transcription.speech.description"),
        )
        speech.update_property([Gtk.AccessibleProperty.LABEL], [text("settings.transcription.speech.a11y")])
        self.whistle = Adw.PreferencesRow(css_classes=["speech-model-selected"])
        row = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        self._speech_group = Gtk.CheckButton()
        radio = self.whistle_radio = Gtk.CheckButton(group=self._speech_group, active=True)
        radio.connect("toggled", self._speech_selected, "whistle")
        radio.update_property([Gtk.AccessibleProperty.LABEL], [text("models.whistle.name")])
        row.append(radio)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        name = Gtk.Box(spacing=8)
        name.append(Gtk.Label(label=text("models.whistle.name"), xalign=0, css_classes=["heading"]))
        from .transcription import bundle_path
        from .ux_formats import size

        weights = bundle_path() / "whistle.cact"
        if weights.is_file():
            name.append(Gtk.Label(label=size(weights.stat().st_size), css_classes=["dim-label", "caption"]))
        details.append(name)
        details.append(
            Gtk.Label(
                label=text("models.whistle.description"), wrap=True, xalign=0, css_classes=["dim-label", "caption"]
            )
        )
        self.whistle_status = Gtk.Label(
            label=text("models.status.inUse"), xalign=0, css_classes=["success", "caption", "heading"]
        )
        details.append(self.whistle_status)
        row.append(details)
        row.append(Gtk.Label(label=text("models.included"), css_classes=["dim-label", "caption"]))
        self.whistle.set_child(row)
        speech.add(self.whistle)
        if self.app.speech_models is not None:
            from .parakeet_model import MODEL_ID

            self.parakeet = Adw.ActionRow(
                title=text("models.parakeetUltra.name"),
                subtitle=text("models.parakeetUltra.description"),
                use_markup=False,
                subtitle_lines=0,
            )
            self.parakeet_radio = Gtk.CheckButton(group=self._speech_group)
            self.parakeet_radio.update_property([Gtk.AccessibleProperty.LABEL], [text("models.parakeetUltra.name")])
            self.parakeet_radio.connect("toggled", self._speech_selected, MODEL_ID)
            self.parakeet.add_prefix(self.parakeet_radio)
            self.parakeet.set_activatable_widget(self.parakeet_radio)
            controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
            self.parakeet_status = Gtk.Label(xalign=0)
            controls.append(self.parakeet_status)
            self.parakeet_progress = Gtk.ProgressBar()
            self.parakeet_progress.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [text("models.downloading.a11y", model=text("models.parakeetUltra.name"))],
            )
            controls.append(self.parakeet_progress)
            self.parakeet.add_suffix(controls)
            self.parakeet_action = Gtk.Button(valign=Gtk.Align.CENTER)
            self.parakeet_action.connect("clicked", self._model_action)
            self.parakeet.add_suffix(self.parakeet_action)
            speech.add(self.parakeet)
            self.model_line = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label"])
            speech.add(self.model_line)
            self.render_models()
        page.add(speech)
        # Source/unsupported-architecture builds never claim an absent bundle.
        self.whistle.set_visible(self.app.writing_available)
        network = Adw.PreferencesGroup(title=text("settings.transcription.network.heading"))
        network.add(
            Gtk.Label(
                label="• " + text("settings.transcription.network.download"),
                wrap=True,
                xalign=0,
                css_classes=["dim-label"],
            )
        )
        page.add(network)

    def _speech_selected(self, radio, model):
        if radio.get_active() and not self._updating and self.app.speech_models:
            self.app.speech_models.select(model)
            self.render_models()

    def _model_action(self, *_):
        models = self.app.speech_models
        state = models.snapshot()
        if state["downloading"]:
            models.cancel_download()
        elif state["downloaded"]:
            models.remove()
        else:
            models.download()
        self.render_models()

    def render_models(self):
        models = self.app.speech_models
        if models is None or not hasattr(self, "parakeet"):
            return
        from .parakeet_model import DOWNLOAD_BYTES, MODEL_ID
        from .ux_formats import size

        state = models.snapshot()
        chosen = state["selected"] == MODEL_ID
        self._updating = True
        self.whistle_radio.set_active(not chosen)
        self.parakeet_radio.set_active(chosen)
        self._updating = False
        self.whistle_status.set_visible(not chosen)
        self.whistle.set_css_classes(["speech-model-selected"] if not chosen else [])
        if chosen:
            self.parakeet.add_css_class("speech-model-selected")
        else:
            self.parakeet.remove_css_class("speech-model-selected")
        self.parakeet_radio.set_sensitive(state["downloaded"] and not state["busy"])
        self.parakeet_action.set_sensitive(not state["busy"] or state["downloading"])
        self.parakeet_progress.set_visible(state["downloading"])
        self.parakeet_progress.set_fraction(state["percent"] / 100)
        if state["downloading"]:
            status = text("models.status.downloading", percent=state["percent"])
            action = text("common.cancel")
        elif state["downloaded"]:
            status = text("models.status.inUse" if chosen else "models.status.downloaded")
            action = text("models.remove")
        else:
            status = text("format.size.download", size=size(DOWNLOAD_BYTES))
            action = text("common.tryAgain" if state["error"] else "models.download")
        self.parakeet_status.set_css_classes(["success", "caption", "heading"] if chosen else ["dim-label", "caption"])
        self.parakeet_status.set_label(status)
        self.parakeet_action.set_label(action)
        line = text(state["notice"]) if state["notice"] else ""
        if state["error"]:
            line = text("errors.modelDownloadFailed.line", model=text("models.parakeetUltra.name"))
        self.model_line.set_label(line)
        self.model_line.set_visible(bool(line))

    def _library(self):
        group = Adw.PreferencesGroup(description=text("settings.library.folder.description"))
        self.pages["library"].add(group)
        self.folder = Adw.ActionRow(title=text("settings.library.folder.title"), use_markup=False)
        self.show_folder = Gtk.Button(label=text("settings.library.show"), valign=Gtk.Align.CENTER)
        self.show_folder.connect("clicked", lambda *_: self._show_folder())
        self.move = Gtk.Button(label=text("settings.library.move"), valign=Gtk.Align.CENTER)
        self.move.connect("clicked", lambda *_: self._choose_folder())
        self.move.set_sensitive(True)
        self.folder.add_suffix(self.show_folder)
        self.folder.add_suffix(self.move)
        group.add(self.folder)
        self.move_lines = Adw.PreferencesGroup()
        self.pages["library"].add(self.move_lines)
        self._move_rows = []

    def _browser(self):
        group = Adw.PreferencesGroup(description=text("settings.browser.description"))
        self.pages["browserCompanion"].add(group)
        self.browser = Adw.ActionRow(title=text("settings.browser.status.notSetUp"), use_markup=False)
        setup = Gtk.Button(label=text("meeting.companionCard.setUp"), valign=Gtk.Align.CENTER)
        setup.connect("clicked", lambda *_: self.app.on_companion_setup(parent=self))
        self.browser.add_suffix(setup)
        group.add(self.browser)

    def _show_folder(self):
        root = library_root()
        future = self.executor.submit(root.mkdir, parents=True, exist_ok=True)

        def ready(result):
            def launch():
                if not self.closed:
                    try:
                        result.result()
                        Gtk.FileLauncher(file=Gio.File.new_for_path(str(root))).launch(
                            self.app.get_active_window(), None, None
                        )
                    except OSError:
                        LOG.exception("Could not open Library folder")
                return False

            GLib.idle_add(launch)

        future.add_done_callback(ready)

    def _choose_folder(self):
        chooser = Gtk.FileDialog(
            title=text("settings.library.move"), initial_folder=Gio.File.new_for_path(str(library_root()))
        )
        chooser.select_folder(self.app.get_active_window(), None, self._folder_chosen)

    def _folder_chosen(self, chooser, result):
        try:
            file = chooser.select_folder_finish(result)
        except GLib.Error:
            return
        if file.get_path():
            self.app.move_library(Path(file.get_path()), self)

    def _automatic_changed(self, *_):
        if not self._updating:
            enabled = self.automatic.get_active()
            token = self.app.begin_auto_recording_change(enabled)
            self._request("configure", autoRecord=enabled, _preferenceToken=token)

    def _request(self, action="status", **values):
        if self.closed:
            return
        if self._future:
            if action != "status":
                self._queued = (action, values)
                self.automatic.set_sensitive(False)
            return
        if action != "status":
            self.automatic.set_sensitive(False)

        self._future = self.executor.submit(self._call, action, values)
        self._future.add_done_callback(lambda future: GLib.idle_add(self._received, future))

    def _call(self, action, values):
        values = dict(values)
        token = values.pop("_preferenceToken", None)
        result = None
        try:
            target = browser_service.ensure_service(
                self.service_path, cancelled=self.cancelled if action == "status" else None
            )
            with browser_service.connect(target, timeout=0.8) as connection, connection.makefile("rwb") as stream:
                browser_service.send_packet(stream, dict(role="app", action=action, **values))
                result = browser_service.read_packet(stream)
                if result is None or result.get("error"):
                    raise ValueError("Browser connection unavailable")
                return result
        finally:
            if token is not None:
                GLib.idle_add(self.app.finish_auto_recording_change, token, result)

    def _received(self, future):
        self._future = None
        if self.closed:
            return False
        try:
            self._snapshot = future.result()
        except (OSError, ValueError):
            LOG.exception("Could not update browser preferences")
        self._updating = True
        self.automatic.set_active(self._snapshot.get("autoRecord", False))
        self.automatic.set_sensitive(True)
        self._updating = False
        names = self._snapshot.get("browserNames", [])
        if self._snapshot.get("connectedBrowsers", self._snapshot.get("browsers")):
            if names:
                self.browser.set_title(text("settings.browser.status.connected", browser=", ".join(names)))
            else:
                self.browser.set_title(text("settings.browser.status.connectedGeneric"))
        else:
            self.browser.set_title(text("settings.browser.status.notSetUp"))
        if self._queued:
            (action, values), self._queued = self._queued, None
            self._request(action, **values)
        return False

    def refresh(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        shortcut = self.app.global_shortcut
        self.shortcut.set_subtitle(
            shortcut.trigger_description if shortcut else self.settings.get_string("record-shortcut-description")
        )
        self.change_shortcut.set_sensitive(bool(shortcut and shortcut.bound and shortcut.configure_supported))
        mover = self.app.library_relocator
        self.render_moves(mover.state, moving=mover.running, error=mover._error)
        self._request()
        return GLib.SOURCE_CONTINUE

    def render_moves(self, state, *, moving=False, error=None):
        self.folder.set_subtitle(text("settings.library.move.moving") if moving else path_caption(library_root()))
        self.move.set_sensitive(not moving)
        for row in self._move_rows:
            self.move_lines.remove(row)
        self._move_rows = []
        entries = state["pending"] if state else []
        waiting = sum(entry["state"] == "waiting" for entry in entries)
        if waiting:
            row = Adw.ActionRow(title=plural("settings.library.move.waiting", waiting), use_markup=False)
            self.move_lines.add(row)
            self._move_rows.append(row)
        folders = {}
        for entry in entries:
            if entry["state"] == "failed":
                folder = Path(entry["source"]).parent
                folders[folder] = folders.get(folder, 0) + 1
        if error:
            folders[error["folder"]] = error["count"]
        for folder, count in folders.items():
            row = Adw.ActionRow(
                title=plural("settings.library.move.failed", count, folder=path_caption(folder)),
                use_markup=False,
                css_classes=["warning"],
            )
            retry = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER, sensitive=not moving)
            retry.connect("clicked", lambda *_: self.app.library_relocator.retry())
            row.add_suffix(retry)
            self.move_lines.add(row)
            self._move_rows.append(row)

    def _closed(self, *_):
        self.closed = True
        self.cancelled.set()
        # Preserve the last explicit toggle even when the initial status read
        # has not returned yet. Closing cancels observation, not that choice.
        if self._queued:
            (action, values), self._queued = self._queued, None
            self.executor.submit(self._call, action, values)
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False)
