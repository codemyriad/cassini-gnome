# SPDX-License-Identifier: GPL-3.0-or-later
from cassini_talk.live_transcription import CapturedPCM, DraftTranscript, LiveAudio, LiveTranscription
from cassini_talk.recording_session import RecordingSession


class Recognizer:
    def __init__(self):
        self.windows = []

    def decode_window(self, pcm, rate, channels):
        self.windows.append(len(pcm) // (2 * channels))
        return [{"startMs": 0, "endMs": 400, "text": "word"}]


def block(source, first, frames=16000):
    return CapturedPCM(source, 100, first, 16000, 1, b"\x01\x00" * frames)


def test_interleaved_sources_discontinuity_and_bounded_windows():
    recognizer = Recognizer()
    live = LiveTranscription(recognizer, interval=1, context=1, window=3)
    snapshot = DraftTranscript()
    for source, first, length in (("mic", 0, 64000), ("remote", 0, 16000), ("mic", 80000, 16000)):
        for update in live.accept(block(source, first, length)):
            snapshot.apply(update)
    for update in live.finish():
        snapshot.apply(update)
    assert set(snapshot.snapshot()) == {"mic", "remote"}
    assert [(word.start_ms, word.text) for word in snapshot.snapshot()["mic"][0]] == [(100, "word"), (5100, "word")]
    assert max(recognizer.windows) <= 3 * 16000
    assert snapshot.snapshot()["mic"][1] == 6100


def test_full_queue_repairs_only_missing_audio_from_capture(tmp_path):
    feed = LiveAudio(capacity=1)
    capture = RecordingSession(tmp_path, live_audio=feed)
    capture.add_track("mic", "Mic", "microphone", 16000, 1)
    capture.append("mic", block("mic", 0).data, 100)
    capture.append_native("mic", block("mic", 32000).data, 2100, gap_frames=16000)
    track = capture.manifest["tracks"][0]
    capture.finish()
    paths = {"mic": capture.directory / track["file"]}
    live = LiveTranscription(Recognizer(), interval=1)
    result = live.drain(feed, saved_tracks=paths)
    assert feed.dropped_blocks == 1
    assert not result.needs_saved_audio
    transcript = DraftTranscript()
    for update in result.updates:
        transcript.apply(update)
    assert [word.start_ms for word in transcript.snapshot()["mic"][0]] == [100, 2100]


def test_recording_owns_preview_lifetime(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from threading import Event

    from cassini_talk.live_transcription import CapturePreview

    fixture = Recognizer()
    preview = CapturePreview("whistle")
    done = Event()
    original_status = preview._status

    def status(value, **kwargs):
        original_status(value, **kwargs)
        if value in ("finished", "incomplete", "unavailable"):
            done.set()

    monkeypatch.setattr(preview, "_client", lambda: nullcontext(fixture))
    monkeypatch.setattr(preview, "_status", status)
    capture = RecordingSession(tmp_path, live_audio=preview)
    capture.add_track("mic", "Mic", "microphone", 16000, 1)
    capture.append("mic", block("mic", 0).data, 100)
    capture.finish()
    assert done.wait(5)
    state, draft = preview.snapshot()
    assert state == "finished"
    assert [word.text for word in draft["mic"][0]] == ["word"]


def test_unsupported_companion_rate_does_not_hide_supported_sources():
    audio = LiveAudio()
    audio.offer(CapturedPCM("remote", 0, 0, 44_100, 1, b"\x01\x00" * 44_100))
    audio.offer(block("mic", 0))
    result = LiveTranscription(Recognizer()).drain(audio)
    assert result.needs_saved_audio
    assert [update.source for update in result.updates] == ["mic"]
    assert result.updates[0].words


def test_adjacent_repair_does_not_replace_next_range_words():
    from cassini_talk.live_transcription import DraftUpdate, LiveWord

    transcript = DraftTranscript()
    transcript.apply(DraftUpdate("mic", 1000, 2000, 2000, (LiveWord(1000, 1000, "next"),)))
    transcript.apply(DraftUpdate("mic", 0, 1000, 1000, (LiveWord(200, 400, "repair"),)))
    assert [word.text for word in transcript.snapshot()["mic"][0]] == ["repair", "next"]


def test_ten_ms_capture_blocks_coalesce_without_filling_the_consumer_queue():
    from cassini_talk.live_transcription import BlockCoalescer

    audio = LiveAudio(capacity=32)
    coalesce = BlockCoalescer(audio.offer)
    for frame in range(0, 16_000, 160):
        for source in ("mic", "remote"):
            coalesce.offer(block(source, frame, 160))
    coalesce.flush()
    result = LiveTranscription(Recognizer()).drain(audio)
    assert audio.dropped_blocks == 0
    assert not result.needs_saved_audio
    assert {update.source for update in result.updates} == {"mic", "remote"}


def test_discontinuity_does_not_decode_a_tiny_fragment():
    audio = LiveAudio()
    audio.offer(block("mic", 0, 160))
    audio.offer(block("mic", 16_000, 16_000))
    recognizer = Recognizer()
    result = LiveTranscription(recognizer).drain(audio)
    assert result.needs_saved_audio
    assert recognizer.windows == [16_000]
