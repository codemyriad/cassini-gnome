#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GTK (D-977, Wave 6; D-1078 D103): voices question, guided test phrase, row menu, rename, delete, sample toggle."""

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.settings_dialog import SettingsDialog  # noqa: E402
from cassini_talk.strings import text  # noqa: E402
from cassini_talk.voice_board import seed  # noqa: E402

RATE = 16000


def until(check, message="Wave 6 Saved voices GUI did not settle"):
    deadline = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), message


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    if isinstance(widget, Gtk.Button) and widget.get_label():
        values.append(widget.get_label())
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


def tone(ms):
    return array("f", [0.1 * math.sin(2 * math.pi * 220 * i / RATE) for i in range(ms * RATE // 1000)])


MISSING = dict(
    downloaded=False,
    ready=False,
    busy=False,
    downloading=False,
    error=False,
    runtime_error=False,
    percent=0,
    notice=None,
    selected=None,
)
ON = dict(MISSING, downloaded=True, ready=True, percent=100)


class FakeModels:
    """Optional-model stand-in: records download requests, never downloads."""

    def __init__(self):
        self.state = dict(MISSING)
        self.downloads = 0

    def snapshot(self):
        return dict(self.state)

    def download(self):
        self.downloads += 1
        self.state = dict(self.state, downloading=True)

    def request(self, *_args, **_kwargs):
        return None

    def cancel_download(self):
        pass

    def remove(self):
        pass

    def retry(self):
        pass

    def close(self):
        pass


class FakeRecorder:
    """The microphone stand-in: the test hands it a take; nothing is opened."""

    takes = []

    def __init__(self):
        self.handlers = {}
        self.samples = array("f")
        self.cancelled = False

    def connect(self, name, callback):
        self.handlers[name] = callback

    def start(self):
        self.handlers["level"](self, -20.0)

    def stop(self):
        self.samples = FakeRecorder.takes.pop(0)
        self.handlers["finished"](self, True)

    def cancel(self):
        self.cancelled = True


extracted = []


def fake_extract(_app, samples, windows):
    extracted.append((len(samples), list(windows)))
    return dict(embedding=[float(i == 7) for i in range(512)], seconds=sum(e - s for s, e in windows) / RATE)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )
    app = CassiniApplication("com.gocassini.SavedVoicesW6Test", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    library_before = {p: p.read_bytes() for p in meeting.directory.iterdir() if p.is_file()}
    future = window.monitor._executor.submit(seed, app, meeting)
    until(future.done)
    future.result()
    # No optional weights in GUI tests: stand-ins report model state only.
    original_models = app.diarization_models, app.voice_models
    if app.voice_backfill:
        app.voice_backfill.close()
    app.voice_backfill = None
    tell, voices_model = FakeModels(), FakeModels()
    app.diarization_models, app.voice_models = tell, voices_model
    assert app.recording_settings.get_string("voice-setup") == "unanswered"

    settings = app.settings_dialog = SettingsDialog(app)
    settings.present(window)
    settings.set_visible_page_name("transcription")
    settings._show_voices()
    page = settings.saved_voices_page
    until(lambda: len(page.rows) == 4 and not page._loading)
    shown = labels(page)

    # Privacy line: local only, no cloud claim on the desktop (HARMONY §12, D25, L-63).
    assert text("voices.setup.privacy.desktop") in shown
    assert not any("iCloud" in value or "backup" in value.lower() for value in shown)
    # The voices question left Saved voices (D103 §3); one footnote remains (L-63).
    assert text("voices.setup.title") not in shown and not hasattr(page, "setup")
    assert text("savedVoices.howToAdd.short") in shown
    assert page.everyone.get_label() == "Forget everyone…"
    assert not page.add_voice.get_sensitive() and page.add_voice.get_label() == "Add a voice…"
    assert page.models_needed.get_visible()

    # 1. One voices question over the first meeting with more than one speaker (D103 §3).
    from cassini_talk.voice_question import VoiceQuestion

    settings.force_close()
    assert not app.offer_voice_question(window.meeting_page or SimpleNamespace(meeting=meeting, window=window))
    app.voice_question_enabled = True  # Screenshot runs keep it off unless asked.
    talk = next(
        m
        for m in window.snapshot.meetings
        if len(m.speakers) > 1 and m.metadata_editable and m.transcript not in ("queued", "running")
    )
    transcribing = next(m for m in window.snapshot.meetings if m.transcript in ("queued", "running") and m.speakers)
    window.open_meeting(transcribing)  # Not while it is still being written down.
    until(lambda: window.meeting_page is not None and not window.meeting_page._voice_question_idle)
    assert app.voice_question is None
    window.open_meeting(talk)
    until(lambda: isinstance(app.voice_question, VoiceQuestion) and app.voice_question.get_mapped())
    question = app.voice_question
    assert question.get_heading() == text("voices.setup.title") == "Recognise people you name?"
    assert question.get_body() == text("voices.setup.body.desktop")
    assert question.footnote.get_text() == (
        "It’s kind to ask people before you save their voice. You can change this in Preferences › Transcription."
    )
    assert question.get_response_label("not-now") == "Not now"
    assert question.get_response_label("enable") == "Remember voices"
    assert question.get_default_response() == "enable" and question.get_close_response() == "not-now"
    question.emit("response", "not-now")
    question.force_close()
    until(lambda: app.voice_question is None)
    assert app.recording_settings.get_string("voice-setup") == "declined"
    assert app.recording_settings.get_boolean("remember-voices") is False  # Not now stores off.
    assert tell.downloads == voices_model.downloads == 0  # Not now downloads nothing.
    # Asked once: reopening the meeting never asks again.
    window.meeting_page.update(window.meeting_page.meeting)
    until(lambda: not window.meeting_page._voice_question_idle)
    assert app.voice_question is None
    # Name a speaker follows the answer.
    window.meeting_page.name_speakers()
    speakers = window.meeting_page.speakers_dialog
    assert speakers.remember.get_active() is False
    speakers.force_close()
    until(lambda: speakers.closed)
    # Remember voices: the default turns on and both optional models are asked for.
    app.recording_settings.set_string("voice-setup", "unanswered")
    window.meeting_page.update(window.meeting_page.meeting)
    until(lambda: app.voice_question is not None and app.voice_question.get_mapped())
    question = app.voice_question
    question.emit("response", "enable")
    question.force_close()
    until(lambda: app.voice_question is None)
    assert app.recording_settings.get_string("voice-setup") == "enabled"
    assert app.recording_settings.get_boolean("remember-voices") is True
    assert tell.downloads == 1 and voices_model.downloads == 0  # Tell speakers apart first.
    tell.state = dict(ON)
    app._models_changed()
    assert voices_model.downloads == 1
    voices_model.state = dict(ON)
    app._models_changed()
    assert tell.downloads == 1 and voices_model.downloads == 1  # Never repeats.
    window.meeting_page.name_speakers()
    speakers = window.meeting_page.speakers_dialog
    assert speakers.remember.get_active() is True
    speakers.force_close()
    until(lambda: speakers.closed)

    # Preferences › Transcription › Speakers: Remember people I name (PROVISIONAL, D-1044 q4).
    settings = app.settings_dialog = SettingsDialog(app)
    settings.present(window)
    settings.set_visible_page_name("transcription")
    remember = settings.remember_default
    assert remember.get_title() == "Remember people I name"
    assert remember.get_subtitle() == text("voices.rememberDefault.subtitle")
    assert remember.get_active()
    remember.set_active(False)
    assert app.recording_settings.get_boolean("remember-voices") is False
    assert app.recording_settings.get_string("voice-setup") == "enabled"  # Already answered stays.
    app.recording_settings.set_string("voice-setup", "unanswered")
    remember.set_active(True)
    assert app.recording_settings.get_string("voice-setup") == "enabled"  # Choosing here answers it.
    settings._show_voices()
    page = settings.saved_voices_page
    until(lambda: len(page.rows) == 4 and not page._loading)
    page.refresh()
    assert not page.models_needed.get_visible() and page.add_voice.get_sensitive()

    # 2. Saved voices rows (L-63): slot colours, meta, amber stale line, play, one Options menu.
    snapshot = app.voice_store.snapshot()["voices"]
    by_name = {v["name"]: v for v in snapshot}
    marta, nina = page.rows[by_name["Marta"]["id"]], page.rows[by_name["Nina"]["id"]]
    assert marta.avatar.has_css_class("speaker-avatar-1") and nina.avatar.has_css_class("speaker-avatar-4")
    assert marta.row.get_title() == "Marta" and marta.name.get_text() == "Marta"
    assert marta.meta.get_text().endswith("confirmed speech") and "samples" not in " ".join(labels(marta.row))
    assert "Ready" not in labels(marta.row)
    assert not marta.stale_line.get_visible()
    assert nina.stale_line.get_visible() and nina.stale_line.get_text() == text("savedVoices.row.stale")
    assert nina.stale_line.has_css_class("saved-voice-stale")
    assert not hasattr(marta, "rename") and not hasattr(marta, "delete")  # No inline buttons.
    assert marta.play.get_tooltip_text() == "Play a sample of Marta"
    assert marta.options.get_tooltip_text() == "Options for Marta"
    menu = marta.options.get_menu_model()
    items = [
        (
            menu.get_item_attribute_value(i, "label", None).get_string(),
            menu.get_item_attribute_value(i, "action", None).get_string(),
        )
        for i in range(menu.get_n_items())
    ]
    assert items == [("Play a sample", "voice.play"), ("Rename…", "voice.rename"), ("Forget…", "voice.forget")]
    assert marta.actions.lookup_action("rename").get_enabled() and marta.actions.lookup_action("forget").get_enabled()
    marta.actions.activate_action("forget", None)
    assert page.confirmation.get_heading() == "Delete Marta’s voice profile?"
    page.confirmation.force_close()
    marta.actions.activate_action("rename", None)
    assert page.rename_dialog.get_heading() == "Rename saved voice"
    page.rename_dialog.force_close()
    page._tick()
    assert marta.actions.lookup_action("play").get_enabled() == marta.play.get_sensitive()

    # Sample toggle: Play becomes Stop sample while it plays.
    stopped = []
    page.player = SimpleNamespace(playing=True, _prerolled=False, shutdown=lambda: stopped.append(True))
    page._sample_voice, page._sample_pending = by_name["Marta"]["id"], (0, 1)
    page._tick()
    assert marta.play.get_tooltip_text() == "Stop sample of Marta"
    assert marta.play.get_icon_name() == "media-playback-stop-symbolic"
    page._toggle_sample(by_name["Marta"]["id"])
    assert stopped and page.player is None
    page._tick()
    assert marta.play.get_tooltip_text() == "Play a sample of Marta"

    # 3. Guided test-phrase enrollment.
    with (
        patch("cassini_talk.voice_enroll_dialog.PhraseRecorder", FakeRecorder),
        patch("cassini_talk.voice_enroll_dialog.extract_print", fake_extract),
    ):
        page.add_voice.emit("clicked")
        dialog = page.enroll_dialog
        until(lambda: dialog.get_mapped())
        shown = labels(dialog)
        for identifier in (
            "voices.enroll.instructions",
            "voices.enroll.testPhraseLabel",
            "voices.enroll.testPhrase",
            "voices.enroll.fromTranscriptHint",
        ):
            assert text(identifier) in shown, identifier
        assert dialog.get_title() == "Add a saved voice"
        assert dialog.record.get_label() == "Record sample" and dialog.save.get_label() == "Save voice"
        assert not dialog.save.get_sensitive()
        # No take while a recording runs.
        with patch.object(app, "recording_active", return_value=True):
            dialog._render()
            assert not dialog.record.get_sensitive()
            dialog.record.emit("clicked")
            assert dialog.recorder is None
        dialog._render()
        dialog.entry.set_text("Bea")
        FakeRecorder.takes = [tone(2500)]
        dialog.record.emit("clicked")
        assert dialog.status.get_text() == text("voices.enroll.recording") and dialog.record.get_label() == "Stop"
        assert dialog.level.get_visible()
        dialog.record.emit("clicked")
        until(lambda: dialog.state == "too-short")
        assert dialog.status.get_text() == text("voices.enroll.tooShort") and not dialog.save.get_sensitive()
        assert not extracted  # Under 4 s never reaches the CAM++ child.
        FakeRecorder.takes = [tone(6000)]
        dialog.record.emit("clicked")
        dialog.record.emit("clicked")
        until(lambda: dialog.state == "ready")
        assert dialog.status.get_text() == "Voice sample ready." and dialog.save.get_sensitive()
        assert extracted and extracted[-1][1] and all(e - s <= 10 * RATE for s, e in extracted[-1][1])
        # A typed saved name asks first (D25 duplicate rule).
        dialog.entry.set_text("marta")
        assert dialog.duplicate.get_visible() and not dialog.save.get_sensitive()
        assert dialog.duplicate_title.get_text() == "Marta is already a saved voice."
        dialog.entry.set_text("Bea")
        assert not dialog.duplicate.get_visible()
        dialog.save.emit("clicked")
        until(lambda: dialog.closed)
        until(lambda: len(page.rows) == 5)
        bea = next(v for v in app.voice_store.snapshot()["voices"] if v["name"] == "Bea")
        assert bea["meetings"] == [] and bea["count"] == 1 and bea["saveOrder"] == 4
        bea_row = page.rows[bea["id"]]
        assert bea_row.avatar.has_css_class("speaker-avatar-5")
        assert bea_row.meta.get_text().startswith("0 meetings")
        assert not bea_row.play.get_sensitive()  # No audio is stored for a voice.
        # Same person adds a sample to the existing voice.
        before = by_name["Marta"]["count"]
        page.enroll()
        dialog = page.enroll_dialog
        until(lambda: dialog.get_mapped())
        dialog.entry.set_text("Marta")
        FakeRecorder.takes = [tone(6000)]
        dialog.record.emit("clicked")
        dialog.record.emit("clicked")
        until(lambda: dialog.state == "ready")
        assert not dialog.save.get_sensitive()
        dialog.same_person.emit("clicked")
        assert dialog.save.get_sensitive()
        dialog.save.emit("clicked")
        until(lambda: dialog.closed)
        until(
            lambda: next(v for v in app.voice_store.snapshot()["voices"] if v["name"] == "Marta")["count"] == before + 1
        )
        # Closing mid-take drops it.
        page.enroll()
        dialog = page.enroll_dialog
        until(lambda: dialog.get_mapped())
        dialog.record.emit("clicked")
        recorder = dialog.recorder
        dialog.force_close()
        until(lambda: dialog.closed)
        assert recorder.cancelled and dialog.print is None

    # 4. Rename.
    page.rename(bea)
    rename = page.rename_dialog
    until(lambda: rename.get_mapped())
    assert rename.get_heading() == "Rename saved voice" and rename.get_response_label("save") == "Save"
    rename.entry.set_text("   ")
    assert rename.blank.get_visible() and rename.blank.get_text() == "Enter a name."
    assert not rename.get_response_enabled("save")
    rename.entry.set_text("Bea Rossi")
    assert rename.get_response_enabled("save") and not rename.blank.get_visible()
    rename.emit("response", "save")
    rename.force_close()
    until(lambda: not page.busy and page.rows[bea["id"]].row.get_title() == "Bea Rossi")
    assert page.rows[bea["id"]].avatar.get_label() == "BR"

    # 5. Delete one voice, then Forget everyone; names in meetings stay.
    renamed = next(v for v in app.voice_store.snapshot()["voices"] if v["id"] == bea["id"])
    page.confirm(renamed)
    assert page.confirmation.get_heading() == "Delete Bea Rossi’s voice profile?"
    assert page.confirmation.get_body() == text("confirm.deleteVoiceProfile.body", name="Bea Rossi")
    assert page.confirmation.get_response_label("forget") == "Delete"
    page.confirmation.emit("response", "forget")
    page.confirmation.force_close()
    until(lambda: not page.busy and bea["id"] not in page.rows)
    page.confirm(None)
    assert page.confirmation.get_heading() == "Forget all saved voices?"
    assert page.confirmation.get_response_label("forget") == "Forget everyone"
    page.confirmation.emit("response", "forget")
    page.confirmation.force_close()
    until(lambda: not page.busy and not page.rows)
    assert page.empty.get_visible() and not page.everyone.get_sensitive()

    # Nothing in the Library meeting changed except names the person typed (none here).
    after = {p: p.read_bytes() for p in meeting.directory.iterdir() if p.is_file()}
    assert {p for p in after if p not in library_before} <= {meeting.directory / "meeting.json"}
    assert not any(p.suffix == ".f32" for p in meeting.directory.iterdir())
    settings.force_close()
    app.diarization_models, app.voice_models = original_models
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS D-977/D-1078 GTK: voices question over the meeting (Not now / Remember voices, asked once), "
        "Remember people I name, guided test phrase (too short, ready, duplicate, same person, cancel), Options menu, "
        "rename, delete, forget everyone, stale line, sample toggle, privacy line"
    )
