#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wave 1 token review boards: delete dialog, error line, success badge, and 7 speakers (1 unnamed)."""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from dispatch_wait import dispatch_until, drain_app_workers  # noqa: E402
from gi.repository import Adw, Gio, Gtk  # noqa: E402
from painted import painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.companion_setup import CompanionSetup  # noqa: E402
from cassini_talk.library import LibrarySpeaker  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402
from cassini_talk.settings_dialog import SettingsDialog  # noqa: E402
from cassini_talk.tokens import speaker_colour, speaker_slot, token  # noqa: E402
from cassini_talk.voice_board import seed  # noqa: E402


def until(check, message: str = "W1 token board did not settle") -> None:
    dispatch_until(check, message)


def save_shot(window, out_dir: Path, name: str) -> None:
    frames: list[int] = []
    window.add_tick_callback(lambda *_: (frames.append(1), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2, f"{name} frame settle timed out")
    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    dest = out_dir / f"w1-{name}.png"
    assert texture.save_to_png(str(dest))


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    out_dir = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    dark = os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"

    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml",
        schemas / "io.codemyriad.CassiniTalk.gschema.xml",
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    fixtures = tmp / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(fixtures / "main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(fixtures / "main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_cursor_blink = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)

    app = CassiniApplication("com.gocassini.W1Tokens", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and (window.get_width(), window.get_height()) == (1120, 720))
    window.monitor.close()

    # 1. Seven-speaker meeting with 6 named speakers (slots 1..6) + 1 unnamed speaker (slot 0 stone).
    base_meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    seven_speakers = (
        LibrarySpeaker("s1", "Marta", "user", 48000, 0),
        LibrarySpeaker("s2", "Luca", "user", 41000, 1),
        LibrarySpeaker("s3", "Priya", "user", 36000, 2),
        LibrarySpeaker("s4", "Jonas", "user", 31000, 3),
        LibrarySpeaker("s5", "Sora", "user", 27000, 4),
        LibrarySpeaker("s6", "Elena", "user", 22000, 5),
        LibrarySpeaker("s7", "Speaker 7", "unnamed", 15000, 6),
    )
    assert [speaker_slot(s) for s in seven_speakers] == [1, 2, 3, 4, 5, 6, 0]
    assert speaker_slot(LibrarySpeaker("s8", "Nikos", "user", 10000, 6)) == 1
    lines = (
        ("s1", "Let us confirm the linen surface and teal accent across every reading board."),
        ("s2", "Primary actions stay filled teal while playback and search share the highlight wash."),
        ("s3", "Degraded states like a silent microphone stay in warm amber."),
        ("s4", "Recording pills and live indicators keep their dedicated red signal."),
        ("s5", "Saved-voice badges pair the six soft avatar washes with their matching speaker inks."),
        ("s6", "Inline download failures and destructive confirmation text use the error role."),
        ("s7", "An unnamed speaker stays neutral stone until someone gives them a name."),
    )
    words = tuple(
        Word(spk, idx * 4000 + w_i * 350, idx * 4000 + w_i * 350 + 300, tok)
        for idx, (spk, sentence) in enumerate(lines)
        for w_i, tok in enumerate(sentence.split())
    )
    seven_meeting = replace(base_meeting, speakers=seven_speakers, words=words, turns=reading_turns(words))
    snap = window._source_snapshot or window.snapshot
    window._library_scanned(
        replace(snap, meetings=tuple(seven_meeting if m.key == seven_meeting.key else m for m in snap.meetings))
    )
    window._activity_tick()
    window.open_meeting(seven_meeting)
    page = window.meeting_page
    until(lambda: page.list.ready and len(page.bound) == 7)
    for spk in seven_speakers:
        expected = speaker_colour(spk, dark=dark)
        if spk.provenance == "unnamed":
            assert expected == token("speaker-0", dark=dark)
    save_shot(window, out_dir, "seven-speakers")

    # 2. Error line in Settings -> Transcription (HARMONY 10.4 --error-color).
    fake_speech = SimpleNamespace(
        snapshot=lambda: dict(
            selected="whistle-1",
            downloaded=False,
            downloading=False,
            busy=False,
            percent=0,
            error=True,
            notice=None,
        ),
        refresh_suggestion=lambda: None,
    )
    with patch.object(app, "speech_models", fake_speech):
        settings = app.settings_dialog = SettingsDialog(app)
        settings.present(window)
        settings.set_visible_page_name("transcription")
        settings.render_models()
        until(lambda: settings.model_line.get_visible() and "error" in settings.model_line.get_css_classes())
        save_shot(window, out_dir, "error-line")

        # 3. Delete confirmation dialog on Saved Voices (HARMONY 10.4 destructive alert + 10.12 avatar-1..6).
        seed(app, base_meeting)
        settings._show_voices()
        voices_page = settings.saved_voices_page
        until(lambda: len(voices_page.rows) >= 3 and not voices_page._loading)
        first_voice = app.voice_store.snapshot()["voices"][0]
        voices_page.confirm(first_voice)
        until(lambda: voices_page.confirmation is not None and voices_page.confirmation.get_mapped())
        save_shot(window, out_dir, "delete-dialog")
        voices_page.confirmation.force_close()
        settings.force_close()

    # 4. Success badge in Browser Companion setup (HARMONY 10.7 success-fill + on-record).
    fake_browser = SimpleNamespace(
        snapshot=lambda: dict(
            phase="connected",
            names=("Firefox",),
            browser=None,
            everConnected=True,
            peers=(),
        )
    )
    with (
        patch.object(app, "browser_connection", fake_browser),
        patch("cassini_talk.companion_setup.setup_complete", return_value=True),
    ):
        companion_dialog = CompanionSetup(app)
        companion_dialog.present(window)
        companion_dialog.render()
        until(
            lambda: (
                "done" in companion_dialog.add_badge.get_css_classes()
                and "done" in companion_dialog.connect_badge.get_css_classes()
            )
        )
        save_shot(window, out_dir, "success-badge")
        companion_dialog.force_close()

    drain_app_workers(app)
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        f"PASS W1 token states ({'dark' if dark else 'light'}): seven-speakers, error-line, delete-dialog, success-badge"
    )
