# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop merge selection supplements, leaving all Android vectors unchanged."""

from cassini_talk.voices_logic import select_windows
from cassini_talk.worker_voices import group_words


def test_merged_native_voices_keep_both_original_voices_speech():
    turns = [
        dict(speaker=0, startMs=0, endMs=6000),
        dict(speaker=1, startMs=6000, endMs=12000),
        dict(speaker=2, startMs=4000, endMs=7000),
    ]
    words = [dict(speaker="others~1", startMs=0, endMs=6000), dict(speaker="others~3", startMs=6000, endMs=12000)]
    selected, labels = group_words(dict(id="others"), words, turns, {"others~3": "others~1"})
    assert labels == {"others~1": {0, 1}}
    windows = select_windows(turns, selected, 16000, 192000, own_labels=labels)
    assert windows["others~1"] == [(3200, 60800), (115200, 188800)]


def test_different_audio_groups_cannot_steal_words_with_similar_prefixes():
    words = [dict(speaker="track-a", startMs=0, endMs=6000), dict(speaker="track-ab", startMs=0, endMs=6000)]
    selected, labels = group_words(dict(id="track-a"), words, [], {})
    assert selected == words[:1] and labels == {}


def test_historical_group_preserves_embedded_ids_without_stealing_microphone_words():
    words = [dict(speaker=s, startMs=0, endMs=6000) for s in ("guest", "me", "me~1")]
    selected, labels = group_words(dict(id="others"), words, [], {}, all_words=True, excluded_groups=("me",))
    assert selected == words[:1] and labels == {}


def test_historical_reader_supports_embedded_received_transcripts_without_a_sidecar(tmp_path):
    from test_library import fingerprint, received

    from cassini_talk.library import LibraryScanner
    from cassini_talk.worker_voices import historical_words

    directory = received(tmp_path)
    before = fingerprint(directory)
    assert not (directory / "transcript.json").exists()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert historical_words(directory) == [
        dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms) for w in meeting.words
    ]
    assert historical_words(directory) and fingerprint(directory) == before


def test_one_merged_speaker_budget_alternates_groups_and_recording_thirds():
    from cassini_talk.worker_voices import combined_windows

    plans = [("group-a", start * 16000, (start + 10) * 16000) for start in (0, 10, 20, 30, 40, 50)]
    plans += [("group-b", start * 16000, (start + 10) * 16000) for start in (60, 70, 80)]
    selected = combined_windows(plans, 90 * 16000)
    assert sum(e - s for _, s, e in selected) == 45 * 16000
    assert any(group == "group-b" for group, _, _ in selected)
    assert all(1500 * 16 <= end - start <= 10000 * 16 for _, start, end in selected)


def test_two_short_merged_group_runs_can_together_make_a_print():
    selected = []
    for group in ("a", "b"):
        words = [dict(speaker="merged", startMs=0, endMs=3000)]
        windows = select_windows([], words, 16000, 48000, min_seconds=0, max_seconds=None)
        selected += [(group, s, e) for s, e in windows["merged"]]
    from cassini_talk.worker_voices import combined_windows

    assert sum(e - s for _, s, e in combined_windows(selected, 48000)) == 5200 * 16
