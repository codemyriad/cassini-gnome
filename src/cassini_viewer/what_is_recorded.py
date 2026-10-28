# SPDX-License-Identifier: GPL-3.0-or-later
"""UX11: apply source choices from now on; keep the last confirmed choice on failure."""

from gi.repository import Adw, GLib, Gtk

from .inline_line import InlineLine
from .session_events import source_state
from .strings import text


def explain_fallback(parent):
    dialog = Adw.AlertDialog(
        heading=text("errors.othersFallback.why.title"), body=text("errors.othersFallback.why.body")
    )
    dialog.add_response("close", text("common.close"))
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    dialog.present(parent)
    return dialog


class WhatIsRecorded(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title=text("whatIsRecorded.title"), content_width=480)
        self.page, self.app = page, page.app
        self.current = self.app.comfort.sources(page.meeting)
        self.future = None
        self.closed = self.guard = False
        self.failed_choice = None
        self.connect("closed", lambda *_: setattr(self, "closed", True))
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=26,
            margin_end=26,
            margin_top=26,
            margin_bottom=22,
        )
        self.set_child(content)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading.append(Gtk.Label(label=text("whatIsRecorded.title"), css_classes=["title-2"], wrap=True))
        heading.append(
            Gtk.Label(
                label=text("whatIsRecorded.subtitle"),
                wrap=True,
                justify=Gtk.Justification.CENTER,
                css_classes=["dim-label"],
            )
        )
        content.append(heading)
        self.choices = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            css_classes=["boxed-list"],
            accessible_role=Gtk.AccessibleRole.RADIO_GROUP,
        )
        self.choices.update_property([Gtk.AccessibleProperty.LABEL], [text("whatIsRecorded.choices.a11y")])
        self.radios = {}
        group = None
        for choice, title, caption in (
            ((True, False), "whatIsRecorded.me", "common.yourMicrophone"),
            ((False, True), "common.theOthers", "whatIsRecorded.theOthers.caption"),
            ((True, True), "whatIsRecorded.both", "whatIsRecorded.both.caption"),
        ):
            radio = Gtk.CheckButton(group=group, accessible_role=Gtk.AccessibleRole.RADIO)
            group = radio
            radio.update_property([Gtk.AccessibleProperty.LABEL], [text(title)])
            row = Adw.ActionRow(title=text(title), subtitle=text(caption), activatable_widget=radio)
            row.add_prefix(radio)
            self.choices.append(row)
            self.radios[choice] = radio
            radio.connect("toggled", self._selected, choice)
        content.append(self.choices)
        path = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        path.append(
            Gtk.Label(
                label=text("whatIsRecorded.othersHeard.heading"),
                xalign=0,
                css_classes=["small", "heading", "dim-label"],
            )
        )
        card = Gtk.Box(spacing=12, css_classes=["recording-path-card"])
        card.append(
            Gtk.Image(
                icon_name="object-select-symbolic",
                valign=Gtk.Align.START,
                css_classes=["accent"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.path_title = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        self.path_description = Gtk.Label(xalign=0, wrap=True, css_classes=["small", "dim-label"])
        labels.append(self.path_title)
        labels.append(self.path_description)
        card.append(labels)
        path.append(card)
        content.append(path)
        self.failure = InlineLine("whatIsRecorded.changeFailed", action="common.tryAgain", callback=self._retry)
        self.failure.set_visible(False)
        content.append(self.failure)
        self.done = Gtk.Button(label=text("common.done"), css_classes=["suggested-action"], height_request=36)
        self.done.connect("clicked", lambda *_: self.close())
        content.append(self.done)
        self.set_default_widget(self.done)
        self._render()

    def _render(self):
        self.guard = True
        self.radios[(self.current.me, self.current.others)].set_active(True)
        self.guard = False
        if not self.current.others:
            title, description = "whatIsRecorded.othersHeard.none.title", "whatIsRecorded.othersHeard.none.description"
        elif self.current.source == "browser":
            title, description = "common.listeningInsideBrowser", "whatIsRecorded.othersHeard.browser.description"
        else:
            title = "whatIsRecorded.othersHeard.system.title"
            description = (
                "whatIsRecorded.othersHeard.fallback.description"
                if self.current.reason == "lost"
                else "whatIsRecorded.othersHeard.system.description"
            )
        self.path_title.set_label(text(title))
        self.path_description.set_label(text(description))

    def _selected(self, radio, choice):
        if not self.guard and radio.get_active():
            self._apply(choice)

    def _apply(self, choice):
        if self.future is not None or self.closed:
            return
        self.failure.set_visible(False)
        self.failed_choice = choice
        try:
            self.future = self.app.change_recording_sources(self.page.meeting, *choice)
        except (OSError, ValueError):
            self._failed()
            return
        self.choices.set_sensitive(False)
        self.future.add_done_callback(lambda f: GLib.idle_add(self._completed, f))

    def _failed(self):
        self._render()
        self.failure.set_visible(True)

    def _completed(self, future):
        self.future = None
        if self.closed:
            return False
        self.choices.set_sensitive(True)
        try:
            result = future.result()
            self.current = source_state(result["events"]) if isinstance(result, dict) else result
        except Exception:
            self._failed()
        else:
            self._render()
        return False

    def update(self, meeting):
        if self.future is None and not self.closed:
            self.current = self.app.comfort.sources(meeting)
            self._render()

    def _retry(self):
        if self.failed_choice is not None:
            self._apply(self.failed_choice)
