# SPDX-License-Identifier: GPL-3.0-or-later
"""L-11 (D99 §2, PARITY §2): one single-meeting export set, text formats and audio-only rules."""

import os
import threading
import time
from concurrent.futures import CancelledError
from dataclasses import replace
from itertools import pairwise

import pytest
from test_export import ASSETS

from cassini_talk import portable
from cassini_talk.export_model import (
    CUE_LINE,
    CUE_MAX_MS,
    available_formats,
    cues,
    markdown,
    model_projection,
    plain_text,
    srt,
    webvtt,
)
from cassini_talk.exporting import export_meeting
from cassini_talk.library import LibrarySpeaker
from cassini_talk.open_meeting import load_open_meeting
from cassini_talk.received import import_received


def value(items, speakers=(("a", "Ada"), ("b", "Ben")), **extra):
    return dict(
        title="Budget review",
        speakers=[dict(id=s, name=n) for s, n in speakers],
        items=[dict(speaker=s, startMs=start, endMs=end, text=shown) for s, start, end, shown in items],
        verbatim=True,
        **extra,
    )


TWO = value(
    [
        ("a", 0, 400, "Hello"),
        ("a", 400, 900, "everyone."),
        ("b", 3_725_000, 3_725_500, "Thanks,"),
        ("b", 3_725_500, 3_726_000, "Ada."),
    ]
)


def meeting(tmp_path, transcript="done", **changes):
    original = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    words = (
        portable.Word("a", 0, 400, "Hello", text_is_stored=True),
        portable.Word("b", 1000, 1500, "Hi", text_is_stored=True),
    )
    speakers = (LibrarySpeaker("a", "Ada", "recognised", 0, 0), LibrarySpeaker("b", "Speaker 2", "unnamed", 1000, 1))
    return replace(original, transcript=transcript, words=words, speakers=speakers, title="Budget review", **changes)


# --- Plain text -------------------------------------------------------------------------------


def test_plain_text_is_one_block_per_turn_with_name_and_hh_mm_ss():
    assert plain_text(TWO) == "Ada · 00:00:00\nHello everyone.\n\nBen · 01:02:05\nThanks, Ada.\n"


def test_plain_text_unnamed_speakers_are_speaker_n_in_speaking_order():
    result = plain_text(value([("x", 2000, 2400, "later"), ("y", 0, 400, "first")], speakers=()))
    assert result == "Speaker 1 · 00:00:00\nfirst\n\nSpeaker 2 · 00:00:02\nlater\n"


def test_plain_text_never_breaks_a_block_with_control_characters_in_names_or_words():
    result = plain_text(value([("a", 0, 400, "one\n\ntwo\tthree")], speakers=(("a", "Ada\nLovelace"),)))
    assert result == "Ada Lovelace · 00:00:00\none two three\n"


def test_plain_text_keeps_inline_replies_like_the_viewer():
    items = [("a", 0, 1000, "He said yes"), ("b", 900, 1100, "okay"), ("a", 1050, 2000, "then no")]
    assert plain_text(value(items)) == "Ada · 00:00:00\nHe said yes (Ben: okay) then no\n"
    # Subtitles give the reply its own cue for its own speaker.
    assert [(label, start) for label, start, *_ in cues(value(items))] == [("Ada", 0), ("Ben", 900)]


def test_empty_completed_transcript_gives_empty_but_valid_files():
    empty = value([])
    assert plain_text(empty) == "" and srt(empty) == "" and webvtt(empty) == "WEBVTT\n"


# --- SRT --------------------------------------------------------------------------------------


def test_srt_numbers_cues_and_prefixes_every_cue_with_the_name():
    assert srt(TWO) == (
        "1\n00:00:00,000 --> 00:00:01,000\nAda: Hello everyone.\n\n"
        "2\n01:02:05,000 --> 01:02:06,000\nBen: Thanks, Ada.\n"
    )


def test_srt_cues_last_at_most_seven_seconds_and_two_lines():
    words = [("a", i * 450, i * 450 + 400, f"word{i}") for i in range(60)]
    built = cues(value(words))
    assert len(built) > 3
    for _label, start, end, lines, _prefix in built:
        assert 0 < end - start <= CUE_MAX_MS
        assert 1 <= len(lines) <= 2 and all(len(line) <= CUE_LINE for line in lines)
    assert built[0][3][0].startswith("Ada: word0")
    # Every word appears once, in order.
    spoken = " ".join(" ".join(lines)[prefix:] for _l, _s, _e, lines, prefix in built)
    assert spoken.split() == [w[3] for w in words]


def test_srt_a_pause_starts_a_new_cue_and_cues_never_overlap_their_own_next_cue():
    built = cues(value([("a", 0, 100, "one"), ("a", 2000, 2100, "two"), ("a", 2200, 2300, "three")]))
    assert [(start, end, lines) for _l, start, end, lines, _p in built] == [
        (0, 1000, ["Ada: one"]),
        (2000, 3000, ["Ada: two three"]),
    ]
    tight = cues(value([("a", 0, 100, "one"), ("a", 1700, 1800, "two")]))
    assert tight[0][2] == 1000 and tight[1][1] == 1700
    close = cues(value([("a", 0, 100, "one"), ("a", 1600, 1700, "two")]))
    assert close[0][2] <= close[1][1]


def test_srt_speaker_change_starts_a_cue_sorted_by_start():
    built = cues(value([("b", 5000, 5400, "late"), ("a", 0, 400, "early")]))
    assert [(label, start) for label, start, *_ in built] == [("Ada", 0), ("Ben", 5000)]


# --- WebVTT -----------------------------------------------------------------------------------


def test_webvtt_has_header_and_voice_spans():
    assert webvtt(TWO) == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<v Ada>Hello everyone.</v>\n"
        "\n01:02:05.000 --> 01:02:06.000\n<v Ben>Thanks, Ada.</v>\n"
    )


def test_webvtt_escapes_markup_in_speaker_names_and_words():
    hostile = value(
        [("a", 0, 400, "<b>bold</b>"), ("a", 400, 800, "R&D"), ("a", 800, 1200, "-->")],
        speakers=(("a", "Tom & <Jerry>\n-->"),),
    )
    result = webvtt(hostile)
    cue = result.split("\n\n", 1)[1]
    assert (
        cue
        == "00:00:00.000 --> 00:00:01.200\n<v Tom &amp; &lt;Jerry&gt; --&gt;>&lt;b&gt;bold&lt;/b&gt; R&amp;D --&gt;</v>\n"
    )
    # Exactly one timing line: no name or word can forge another cue.
    assert result.count("-->") == 1 and "<b>" not in result and "<Jerry>" not in result


def test_webvtt_keeps_two_line_cues_inside_the_voice_span():
    words = [
        ("a", i * 100, i * 100 + 90, w)
        for i, w in enumerate(["a", "fairly", "long", "sentence", "that", "needs", "two", "lines"])
    ]
    words += [("a", 1000, 1090, "here"), ("a", 1100, 1190, "today")]
    result = webvtt(value(words, speakers=(("a", "Ada"),)))
    body = result.split("\n", 3)[3]
    assert body.startswith("<v Ada>") and body.endswith("</v>\n") and body.count("\n") == 2


# --- D25 voiceprints, names exactly as shown --------------------------------------------------


def test_text_formats_never_carry_voiceprints_or_private_metadata(tmp_path):
    model = meeting(
        tmp_path,
        metadata={"speakers": [{"id": "a", "voiceprint": [0.125, 0.5], "embedding": "secret-print"}]},
        extensions={"codemyriad.io": {"voiceprint": "secret-print"}},
    )
    projection = model_projection(model)
    assert projection["speakers"] == [dict(id="a", name="Ada"), dict(id="b", name="Speaker 2")]
    for writer in (markdown, plain_text, srt, webvtt):
        output = writer(projection)
        assert "secret-print" not in output and "0.125" not in output and "voiceprint" not in output.lower()
        assert "Ada" in output and "Speaker 2" in output


# --- Which formats a meeting offers -----------------------------------------------------------


@pytest.mark.parametrize("state", ["none", "queued", "running", "failed", "damaged"])
def test_audio_only_meetings_offer_only_audio(tmp_path, state):
    own = replace(meeting(tmp_path, transcript=state), origin="own", plain_audio=False)
    assert available_formats(own) == ("cassini",)
    imported_cassini = replace(meeting(tmp_path, transcript=state), plain_audio=False)
    assert available_formats(imported_cassini) == ("cassini",)
    plain = replace(meeting(tmp_path, transcript=state), plain_audio=True)
    assert available_formats(plain) == ("original",)


def test_transcribed_meetings_offer_cassini_markdown_then_more_formats(tmp_path):
    for plain in (False, True):
        model = replace(meeting(tmp_path), plain_audio=plain)
        assert available_formats(model) == ("cassini", "markdown", "txt", "srt", "vtt")


def test_library_marks_plain_imports_and_not_imported_cassini_files(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    plain = import_received(ASSETS / "plain-title.opus", root)
    assert plain.plain_audio and available_formats(plain) == ("original",)
    assert load_open_meeting(ASSETS / "plain-title.opus", root).plain_audio
    cassini_file = export_meeting(plain, tmp_path / "Documents")
    reopened = load_open_meeting(cassini_file, root)
    assert not reopened.plain_audio and available_formats(reopened) == ("cassini",)


# --- Writing files ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,suffix,head", [("txt", ".txt", "Ada · "), ("srt", ".srt", "1\n"), ("vtt", ".vtt", "WEBVTT\n")]
)
def test_text_exports_are_atomic_and_never_replace_user_files(tmp_path, kind, suffix, head):
    directory = tmp_path / "Documents"
    directory.mkdir()
    existing = directory / ("Budget review" + suffix)
    existing.write_text("Existing user file")
    model = meeting(tmp_path)
    first = export_meeting(model, directory, kind)
    second = export_meeting(model, directory, kind)
    assert existing.read_text() == "Existing user file"
    assert first.name == "Budget review 2" + suffix and second.name == "Budget review 3" + suffix
    assert first.read_text().startswith(head) and first.read_bytes() == second.read_bytes()
    assert not list(directory.glob(".cassini-*"))


@pytest.mark.parametrize("kind", ["markdown", "txt", "srt", "vtt"])
@pytest.mark.parametrize("state", ["none", "queued", "running", "failed"])
def test_text_exports_refuse_meetings_without_a_completed_transcript(tmp_path, kind, state):
    directory = tmp_path / "Documents"
    with pytest.raises(ValueError):
        export_meeting(meeting(tmp_path, transcript=state), directory, kind)
    assert not directory.exists() or not list(directory.iterdir())


def test_text_export_is_fsynced_before_publication(tmp_path, monkeypatch):
    from cassini_talk import exporting

    events = []
    real_fsync, real_publish = os.fsync, exporting._publish
    monkeypatch.setattr(exporting.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(exporting, "_publish", lambda *args: (events.append("publish"), real_publish(*args))[1])
    export_meeting(meeting(tmp_path), tmp_path / "Documents", "srt")
    assert events[:2] == ["fsync", "publish"] and events[-1] == "fsync"  # file, then directory


def test_original_audio_is_the_imported_bytes_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    imported = import_received(ASSETS / "plain-title.opus", root)
    path = export_meeting(imported, tmp_path / "Documents", "original")
    assert path.read_bytes() == imported.audio_file.read_bytes() == (ASSETS / "plain-title.opus").read_bytes()
    assert path.suffix == ".opus" and not list(path.parent.glob(".cassini-*"))


def test_original_audio_is_only_for_imported_meetings(tmp_path):
    with pytest.raises(ValueError):
        export_meeting(replace(meeting(tmp_path), origin="own"), tmp_path / "Documents", "original")
    assert not list((tmp_path / "Documents").iterdir())


def test_original_audio_changing_while_copied_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    from cassini_talk import exporting

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    imported = import_received(ASSETS / "plain-title.opus", root)
    real = exporting.check_cancel

    def grow(cancel):
        with imported.audio_file.open("ab") as stream:
            stream.write(b"x")
        return real(cancel)

    monkeypatch.setattr(exporting, "check_cancel", grow)
    with pytest.raises(ValueError), open(os.devnull, "wb") as sink:
        exporting.copy_original_audio(imported, sink)
    monkeypatch.setattr(exporting, "check_cancel", real)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        export_meeting(imported, tmp_path / "Documents", "original", cancel=cancel)
    assert not list((tmp_path / "Documents").iterdir())


# --- Long inputs (BUG-CLASSES #1, D88: scaling, never wall clock) -----------------------------


def test_subtitles_and_text_scale_linearly_on_long_transcripts():
    def build(n):
        return value(
            [("ab"[(i // 7) % 2], i * 400, i * 400 + 350, f"w{i}" + ("." if i % 9 == 0 else "")) for i in range(n)]
        )

    def cpu(model):
        started = time.thread_time()
        srt(model)
        webvtt(model)
        plain_text(model)
        return time.thread_time() - started

    small, large = build(25_000), build(50_000)
    cpu(small)
    small_cpu, large_cpu = cpu(small), cpu(large)
    assert large_cpu < max(small_cpu, 0.05) * 4


def test_zero_duration_and_empty_items_never_crash_or_duplicate():
    built = cues(value([("a", 500, 500, "Yes"), ("a", 500, 500, ""), ("a", 600, 600, ",")], speakers=(("a", "Ada"),)))
    assert len(built) == 1 and built[0][1:3] == (500, 1500)
    assert " ".join(built[0][3]).count("Yes") == 1
    assert srt(value([("a", 0, 0, "")])) == ""  # Nothing audible or shown: no empty cue.


def _timings(text):
    """(start, end) ms of every cue timing line in an SRT or WebVTT document."""

    def ms(stamp):
        clock, millis = stamp.replace(",", ".").rsplit(".", 1)
        hours, minutes, seconds = (int(part) for part in clock.split(":"))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(millis)

    return [tuple(ms(side.strip()) for side in line.split("-->")) for line in text.splitlines() if "-->" in line]


@pytest.mark.parametrize("start,end", [(500, 1500), (500, 500)])
def test_tied_word_timings_split_across_cues_all_stay_visible_in_order(start, end):
    # Imported transcripts can time every word of a long turn identically; length splitting
    # must not make cues start together and hide each other (500 -> 500).
    words = [("a", start, end, f"word{i:02d}") for i in range(40)]
    model = value(words, speakers=(("a", "Ada"),))
    built = cues(model)
    assert len(built) >= 3
    spoken = " ".join(" ".join(lines)[prefix:] for _l, _s, _e, lines, prefix in built)
    assert spoken.split() == [w[3] for w in words]
    for timings in ([(s, e) for _l, s, e, *_ in built], _timings(srt(model)), _timings(webvtt(model))):
        assert len(timings) == len(built)
        assert all(e > s for s, e in timings)
        assert timings[0][0] == start
        # Starts strictly increase and each cue ends by the next one's start: one on screen at a time.
        assert all(a_end <= b_start and a_start < b_start for (a_start, a_end), (b_start, _) in pairwise(timings))
    assert "Ada: word00" in srt(model) and "word39</v>" in webvtt(model)


def test_tied_cues_never_spill_past_the_turns_next_later_cue():
    words = [("a", 500, 600, f"word{i:02d}") for i in range(20)] + [("a", 700, 800, "after")]
    built = cues(value(words, speakers=(("a", "Ada"),)))
    assert all(end > start for _l, start, end, *_ in built)
    assert all(a[2] <= b[1] for a, b in pairwise(built))
    assert built[-1][3][-1].endswith("after")


def test_same_speaker_turns_starting_together_keep_positive_durations():
    # Two separate turns of one speaker at the same instant are only capped by a strictly
    # later start, never collapsed to zero length.
    built = cues(value([("a", 500, 500, "one"), ("b", 500, 500, "x"), ("a", 500, 500, "two")]))
    assert all(end > start for _l, start, end, *_ in built)
