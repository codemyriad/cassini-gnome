# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from cassini_talk.live_transcription import CapturedPCM, DraftTranscript, LiveAudio, LiveTranscription
from cassini_talk.recording_session import RecordingSession


@pytest.fixture(autouse=True)
def numpy_available():
    pytest.importorskip("numpy")


class Recognizer:
    def __init__(self):
        self.windows = []

    def decode_window(self, pcm):
        self.windows.append(len(pcm))
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
