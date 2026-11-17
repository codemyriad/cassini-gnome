# SPDX-License-Identifier: GPL-3.0-or-later
"""D103 (L-58, L-60, L-61, L-65): the Preferences map, first run, Before you record and About copy."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    name: (ROOT / "src/cassini_talk" / name).read_text()
    for name in (
        "settings_dialog.py",
        "settings_map.py",
        "library_window.py",
        "record_first.py",
        "main.py",
        "sound_check_dialog.py",
        "sound_check.py",
    )
}
D103_IDS = (
    "settings.recording.group.starting",
    "settings.recording.firstRecord.subtitle",
    "settings.recording.group.browser",
    "settings.recording.group.browser.subtitle",
    "settings.recording.autoRecord.subtitleShort",
    "settings.recording.test.description",
    "settings.recording.test.button",
    "settings.transcription.privacyLine",
    "models.keepTranscript",
    "models.whistle.description.short",
    "models.parakeetUltra.description.short",
    "models.builtIn",
    "models.statusWithSize",
    "models.tellSpeakersApart.description.short",
    "models.recogniseVoices.description.short",
    "settings.library.folder.descriptionShort",
    "settings.library.stats",
    "library.meetingCount",
    "settings.library.group.keepingCopy",
    "settings.library.backUp.title",
    "settings.library.backUp.subtitle",
    "settings.library.zip.subtitle",
    "settings.library.group.deleted",
    "settings.library.deleted.title",
    "companion.bodyShort",
    "companion.worksWith",
    "companion.addTo",
    "companion.without.heading",
    "companion.without.body",
    "about.tagline.computer",
    "about.help",
    "firstRun.howTo",
    "firstRun.shortcutLine",
    "firstRun.libraryLocation",
    "recordFirst.lawPlain",
    "recordFirst.mic.plain",
    "recordFirst.sound.plain",
    "recordFirst.mic.caption.plain",
    "recordFirst.sound.caption.plain",
)
SUPERSEDED = (
    "settings.recording.autoRecord.subtitle",
    "settings.transcription.privacy",
    "settings.transcription.speech.description",
    "settings.transcription.network.heading",
    "settings.transcription.network.download",
    "models.included",
    "models.whistle.description",
    "models.parakeetUltra.description",
    "models.tellSpeakersApart.description",
    "models.recogniseVoices.description",
    "settings.library.folder.description",
    "settings.library.deleted.body",
    "settings.browser.description",
    "firstRun.body",
    "firstRun.footer",
    "recordFirst.responsibility",
    "recordFirst.mic.description",
    "recordFirst.sound.description",
    "recordFirst.mic.caption",
    "recordFirst.sound.caption",
    "test.intro",
    # D104 supersedes "Test recording first" and the D60 §4 dialog copy.
    "recordFirst.test",
    "test.running",
    "test.result.working",
    "test.result.silent",
)
# D104 (L-61 revised, Test recording): Before you record's Test row and the one-screen test.
D104_SOURCES = ("record_first.py", "sound_check_dialog.py", "sound_check.py")
D104_IDS = (
    "recordFirst.testRow.title",
    "recordFirst.testRow.body",
    "settings.recording.test.button",
    "test.mic.prompt",
    "test.sound.prompt",
    "test.live.hearing",
    "test.live.waiting",
    "test.live.hearingSound",
    "test.pill.working",
    "test.pill.silent",
    "states.badge.recording",
    "test.note",
    "test.playBack",
    "test.again",
    "common.done",
    "common.cancel",
)


def used_ids():
    return {m for source in SOURCES.values() for m in re.findall(r'"([a-zA-Z]+(?:\.[a-zA-Z0-9]+)+)"', source)}


def test_every_d103_id_is_used_and_catalogued_in_all_six_languages():
    entries = {e["id"]: e for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_text())["entries"]}
    used = used_ids()
    for identifier in D103_IDS:
        assert identifier in used, identifier
        entry = entries[identifier]
        assert "gnome" in entry["platforms"], identifier
        for lang in ("it", "fr", "es", "pt-BR", "pt-PT"):
            localized = entry["l10n"][lang]
            assert localized.get("text", "").strip() or localized.get("plural"), (identifier, lang)


def test_every_d104_id_is_used_and_catalogued_in_all_six_languages():
    entries = {e["id"]: e for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_text())["entries"]}
    used = {m for name in D104_SOURCES for m in re.findall(r'"([a-zA-Z]+(?:\.[a-zA-Z0-9]+)+)"', SOURCES[name])}
    for identifier in D104_IDS:
        assert identifier in used, identifier
        entry = entries[identifier]
        assert "gnome" in entry["platforms"], identifier
        for lang in ("it", "fr", "es", "pt-BR", "pt-PT"):
            assert entry["l10n"][lang].get("text", "").strip(), (identifier, lang)


def test_superseded_copy_is_gone_from_these_screens():
    used = used_ids()
    assert not used & set(SUPERSEDED), sorted(used & set(SUPERSEDED))


def test_gnome_keeps_its_in_app_trash_words_in_preferences():
    # D103 §7: GNOME kept its in-app Trash under D101 §3, so the row body is help.trash.body.
    assert '"help.trash.body"' in SOURCES["settings_dialog.py"]


def test_about_links():
    about = SOURCES["main.py"][SOURCES["main.py"].index("def on_about") :]
    about = about[: about.index("about.present(")]
    assert 'website="https://cassini.talk"' in about
    assert 'about.add_link(text("about.help"), "https://cassini.talk/help")' in about
    assert 'comments=text("about.tagline.computer")' in about
    assert "support_url" not in about and "format.gocassini.com" not in about
    assert "issue_url=" in about


def test_installed_speaker_models_read_on_with_their_size():
    from cassini_talk import i18n
    from cassini_talk.settings_map import on_with_size

    with i18n.using_language("en"):
        assert on_with_size(62 * 1048576) == "On · 62 MB"


def test_library_size_skips_hidden_state_and_symlinks(tmp_path):
    from cassini_talk.settings_map import library_usage

    (tmp_path / "a.opus").write_bytes(b"x" * 100)
    session = tmp_path / "Recording-1"
    session.mkdir()
    (session / "mic.wav").write_bytes(b"x" * 20)
    (session / ".partial").write_bytes(b"x" * 3)  # hidden files inside a meeting still count
    trash = tmp_path / ".cassini/trash/entry"
    trash.mkdir(parents=True)
    (trash / "deleted.opus").write_bytes(b"x" * 1000)
    (tmp_path / "link").symlink_to(trash)
    (tmp_path / "file-link.opus").symlink_to(trash / "deleted.opus")
    assert library_usage(tmp_path) == 123
    assert library_usage(tmp_path / "missing") == 0


def test_library_size_counts_v2_working_folders(tmp_path):
    from cassini_talk.settings_map import library_usage

    # Published v2 meeting: the root .opus plus the tracks kept in its working folder.
    (tmp_path / "Standup.opus").write_bytes(b"x" * 100)
    published = tmp_path / ".cassini/sessions/Recording-aaaa"
    (published / "tracks").mkdir(parents=True)
    (published / "session.json").write_bytes(b"x" * 5)
    (published / "tracks/mic.opus").write_bytes(b"x" * 40)
    # Unpublished v2 meeting (recording, interrupted, or low-disk WAVs kept): no root .opus at all.
    unpublished = tmp_path / ".cassini/sessions/Recording-bbbb"
    unpublished.mkdir()
    (unpublished / "mic.wav").write_bytes(b"x" * 2000)
    (unpublished / "system.wav").write_bytes(b"x" * 3000)
    # Not meetings: Trash, staging, journal, a moved-away leftover in sessions.
    for hidden in ("trash/entry/old.opus", "staging/op.opus.stage", "journal/op.json", "sessions/.cassini-moved-1/x"):
        (tmp_path / ".cassini" / hidden).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".cassini" / hidden).write_bytes(b"x" * 10000)
    assert library_usage(tmp_path) == 100 + 5 + 40 + 2000 + 3000


def test_library_size_skips_symlinked_sessions(tmp_path):
    from cassini_talk.settings_map import library_usage

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Recording-1").mkdir(parents=True)
    (elsewhere / "Recording-1/mic.wav").write_bytes(b"x" * 500)
    library = tmp_path / "library"
    (library / ".cassini").mkdir(parents=True)
    (library / ".cassini/sessions").symlink_to(elsewhere)
    assert library_usage(library) == 0
    other = tmp_path / "other"
    other.mkdir()
    (other / ".cassini").symlink_to(library / ".cassini")
    assert library_usage(other) == 0


def test_folder_caption_reads_like_the_board(monkeypatch, tmp_path):
    from cassini_talk.library_location import folder_caption

    monkeypatch.setenv("HOME", str(tmp_path))
    assert folder_caption(tmp_path / "Music/Cassini") == "Music/Cassini"
    assert folder_caption("/srv/library") == "/srv/library"
