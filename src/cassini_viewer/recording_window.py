# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording controls kept alive until the encoder has flushed its output."""

from datetime import datetime
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from .recorder import Recorder


class RecordingWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title=_("Record Meeting"), default_width=480, default_height=420)
        self.open_result = True
        self.recorder = Recorder()
        self.recorder.connect("finished", self._finished)
        self.recorder.connect("error", self._error)
        self._timer = 0
        self._cookie = 0
        self._choosing = False
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self.microphone = Gtk.CheckButton(label=_("Microphone"), active=True)
        self.desktop = Gtk.CheckButton(label=_("Desktop audio"), active=True)
        box.append(self.microphone)
        box.append(self.desktop)
        box.append(
            Gtk.Label(
                label=_(
                    "Desktop audio includes all sounds on your default output. The microphone records even when you are muted in the meeting. Use headphones to avoid echo."
                ),
                wrap=True,
                xalign=0,
            )
        )
        box.append(
            Gtk.Label(
                label=_(
                    "With both sources enabled, microphone is on the left and desktop audio on the right. No transcript is generated."
                ),
                wrap=True,
                xalign=0,
            )
        )
        self.status = Gtk.Label(label=_("Ready to record"), wrap=True)
        box.append(self.status)
        self.button = Gtk.Button(label=_("Record…"), css_classes=["suggested-action"])
        self.button.connect("clicked", self._clicked)
        box.append(self.button)
        view.set_content(box)
        self.set_content(view)
        self.connect("close-request", self._close)

    def _clicked(self, *_args):
        if self.recorder.pipeline is not None:
            self.button.set_sensitive(False)
            self.status.set_label(_("Saving recording…"))
            self.recorder.stop()
            return
        if not self.microphone.get_active() and not self.desktop.get_active():
            self.status.set_label(_("Select at least one audio source."))
            return
        self._choosing = True
        self.button.set_sensitive(False)
        dialog = Gtk.FileDialog(
            title=_("Save Recording"), initial_name=datetime.now().strftime("Meeting %Y-%m-%d %H-%M-%S.opus")
        )
        dialog.save(self, None, self._chosen)

    def _chosen(self, dialog, result):
        self._choosing = False
        self.button.set_sensitive(True)
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        if not file.get_path():
            self.status.set_label(_("Choose a local file for the recording."))
            return
        for window in self.get_application().get_windows():
            if hasattr(window, "player"):
                window.player.pause()
        try:
            self.recorder.start(file.get_path(), self.microphone.get_active(), self.desktop.get_active())
        except Exception as error:
            self._error(self.recorder, str(error))
            return
        self.microphone.set_sensitive(False)
        self.desktop.set_sensitive(False)
        self.button.set_label(_("Stop and Save"))
        self._start_time = GLib.get_monotonic_time()
        self._timer = GLib.timeout_add_seconds(1, self._tick)
        self._cookie = self.get_application().inhibit(
            self, Gtk.ApplicationInhibitFlags.SUSPEND, _("Recording a meeting")
        )
        self._tick()

    def _tick(self):
        elapsed = (GLib.get_monotonic_time() - self._start_time) // 1000000
        if not self.recorder.stopping:
            self.status.set_label(
                _("Recording — {minutes:02d}:{seconds:02d}").format(minutes=elapsed // 60, seconds=elapsed % 60)
            )
        return GLib.SOURCE_CONTINUE

    def _reset(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._cookie:
            self.get_application().uninhibit(self._cookie)
            self._cookie = 0
        self.button.set_sensitive(True)
        self.button.set_label(_("Record…"))
        self.microphone.set_sensitive(True)
        self.desktop.set_sensitive(True)

    def _finished(self, _recorder, path):
        self._reset()
        if self.open_result:
            self.get_application().open_in_new_window(Gio.File.new_for_path(path))
        self.close()

    def _error(self, _recorder, message):
        self._reset()
        self.status.set_label(message)

    def _close(self, *_args):
        if self.recorder.pipeline is not None:
            if not self.recorder.stopping:
                self._clicked()
            return True
        return self._choosing
