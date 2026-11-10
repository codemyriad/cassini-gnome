# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in WebKitGTK 6 transcript, loaded only for the rendering spike."""

import json
import math
import os
import time

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit  # noqa: E402

from .webkit_document import document, row_html  # noqa: E402


class WebKitTranscript(Gtk.Box):
    def __init__(self, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.page = page
        self.ready = False
        self._closed = False
        self._generation = 0
        self._pending_position = None
        self._position_busy = False
        self._rows = ()
        self._roster = None
        self._listening = None
        self.plain = os.environ.get("CASSINI_TRANSCRIPT_WEBKIT_PLAIN") == "1"
        self.tick_cpu_ms = []
        self.manager = WebKit.UserContentManager.new()
        self.manager.register_script_message_handler("cassini", None)
        self._message_handler = self.manager.connect("script-message-received::cassini", self._message)
        self.session = WebKit.NetworkSession.new_ephemeral()
        self.session.set_persistent_credential_storage_enabled(False)
        settings = WebKit.Settings.new()
        for name, value in {
            "enable-page-cache": False,
            "enable-html5-local-storage": False,
            "enable-html5-database": False,
            "enable-dns-prefetching": False,
            "enable-media": False,
            "enable-webrtc": False,
            "enable-webgl": False,
            "enable-developer-extras": False,
        }.items():
            if settings.find_property(name):
                settings.set_property(name, value)
        self.view = WebKit.WebView(network_session=self.session, user_content_manager=self.manager, settings=settings)
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        self._policy_handler = self.view.connect("decide-policy", self._policy)
        self.view.connect("permission-request", lambda _v, request: (request.deny(), True)[1])
        self.view.connect("create", lambda *_: None)
        self.session.connect("download-started", lambda _s, download: download.cancel())
        self.append(self.view)

    def _policy(self, _view, decision, kind):
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == "about:blank":
                decision.use()
            else:
                decision.ignore()
        elif kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
        else:
            return False
        return True

    def _message(self, _manager, value):
        if self._closed:
            return
        try:
            message = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        if message.get("kind") == "ready":
            self.ready = True
            self.position(self.page.player.position)
            self.set_listening(self.page._highlighted_rows)
        elif message.get("kind") == "manual":
            self.page._hand_scroll()
        elif message.get("kind") == "seek":
            position = message.get("ms")
            if (
                isinstance(position, (int, float))
                and math.isfinite(position)
                and 0 <= position <= (self.page.meeting.duration_ms or 0)
            ):
                self.page.player.seek(int(position))

    def evaluate(self, script, callback=None):
        if self._closed:
            return

        def done(view, result):
            try:
                value = view.evaluate_javascript_finish(result)
            except GLib.Error:
                value = None
            if callback and not self._closed:
                callback(value)

        self.view.evaluate_javascript(script, -1, None, None, None, done)

    def update(self, meeting, dark):
        roster = tuple((speaker.id, speaker.name) for speaker in meeting.speakers)
        if self._rows == meeting.turns and self._roster == roster:
            if self.ready:
                self.evaluate(f"cassini.theme({json.dumps(dark)})")
            return

        def names(speaker):
            return meeting.speaker_for(speaker).name

        common = 0
        if self._roster == roster:
            for before, after in zip(self._rows, meeting.turns, strict=False):
                if before != after:
                    break
                common += 1
        if self.ready and common:
            self._listening = None
            tail = "".join(row_html(row, names) for row in meeting.turns[common:])
            self.evaluate(f"cassini.replaceTail({common},{json.dumps(tail)});cassini.theme({json.dumps(dark)})")
        else:
            self.ready = False
            self._listening = None
            self._generation += 1
            self._position_busy = False
            self.view.load_html(document(meeting.turns, names, dark=dark, plain=self.plain), "about:blank")
        self._rows = meeting.turns
        self._roster = roster

    def position(self, position):
        # At most one IPC in flight; a slow WebProcess cannot accumulate ticks.
        self._pending_position = int(position)
        if not self.ready or self._position_busy:
            return
        self._position_busy = True
        target = self._pending_position
        generation = self._generation
        follow = (
            self.page.player.playing
            and self.page.settings.get_boolean("follow-along")
            and GLib.get_monotonic_time() >= self.page._follow_until
        )
        started = time.thread_time_ns()

        def done(_value):
            if generation != self._generation:
                return
            self._position_busy = False
            if self._pending_position != target:
                self.position(self._pending_position)

        self.evaluate(f"cassini.highlight({target},{json.dumps(follow)})", done)
        if len(self.tick_cpu_ms) < 10000:
            self.tick_cpu_ms.append((time.thread_time_ns() - started) / 1e6)

    def scroll_to(self, index, *_):
        if self.ready:
            self.evaluate(f"cassini.follow({int(index)})")

    def set_listening(self, rows):
        state = tuple(sorted(rows))
        if self.ready and state != self._listening:
            self._listening = state
            self.evaluate(f"cassini.listening({json.dumps(state)})")

    def close(self):
        self._closed = True
        self.manager.disconnect(self._message_handler)
        self.view.disconnect(self._policy_handler)
        self.manager.unregister_script_message_handler("cassini", None)
        self.view.stop_loading()
        self.remove(self.view)
        self.page = None
