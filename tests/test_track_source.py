# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §5: every session-track reader accepts WAV spans and compacted Opus spans.

Each fixture builds a real capture (RecordingSession writers, unchanged), then a
twin whose spans are compacted the way §5 describes: the WAV's PCM encoded by
GStreamer's opusenc into `tracks/<span-id>.opus`, the span pointing at it with
`codec`/`decodedRate`, the original `sampleRate`/`frames` kept, the WAV removed.
"""

import array
import json
import math
import os
import shutil
import sys
import time
import wave

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

from cassini_viewer import portable, track_source  # noqa: E402
from cassini_viewer.library import LibraryScanner  # noqa: E402
from cassini_viewer.recording_session import RecordingSession, load_session  # noqa: E402

OPUS_FRAME_MS = 20


def tone(rate, seconds, frequency, channels=1, level=0.3, click_at=None):
    count = round(rate * seconds)
    values = array.array("h")
    for i in range(count):
        value = level * math.sin(2 * math.pi * frequency * i / rate)
        if click_at is not None and abs(i - round(click_at * rate)) < rate // 1000:
            value = 0.9
        values.extend([round(value * 32767)] * channels)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def feed(session, identifier, pcm, offset, rate, channels=1):
    """Append in protocol-sized chunks with consistent timestamps."""
    size = 4800 * channels * 2
    for first in range(0, len(pcm), size):
        session.append(identifier, pcm[first : first + size], offset + first // (channels * 2) * 1000 / rate)


def encode_opus(pcm, rate, channels, output):
    """Compaction stand-in: explicit resampling only when opusenc cannot take the rate."""
    pipeline = Gst.parse_launch(
        f"appsrc name=pcm format=time caps=audio/x-raw,format=S16LE,layout=interleaved,rate={rate},channels={channels} ! "
        "audioconvert ! audioresample ! opusenc bitrate=96000 ! oggmux ! filesink name=output"
    )
    pipeline.get_by_name("output").set_property("location", str(output))
    source = pipeline.get_by_name("pcm")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        buffer = Gst.Buffer.new_wrapped(pcm)
        buffer.pts = 0
        buffer.duration = len(pcm) // (2 * channels) * Gst.SECOND // rate
        assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
        source.emit("end-of-stream")
        message = pipeline.get_bus().timed_pop_filtered(20 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert message and message.type == Gst.MessageType.EOS
    finally:
        pipeline.set_state(Gst.State.NULL)


def compact(directory, identifier, *, keep_wav=False):
    path = directory / "session.json"
    manifest = json.loads(path.read_text())
    span = next(t for t in manifest["tracks"] if t["id"] == identifier)
    wav = directory / span["file"]
    with wave.open(str(wav), "rb") as stream:
        pcm = stream.readframes(stream.getnframes())
        rate, channels = stream.getframerate(), stream.getnchannels()
    (directory / "tracks").mkdir(exist_ok=True)
    encode_opus(pcm, rate, channels, directory / "tracks" / f"{identifier}.opus")
    span.update(codec="opus", file=f"tracks/{identifier}.opus", decodedRate=48000, sourceFile=wav.name)
    path.write_text(json.dumps(manifest))
    if not keep_wav:
        wav.unlink()
    return span


def capture(root):
    session = RecordingSession(root, "Dual readers")
    session.add_track("mic", "Me", "microphone", 16000, 1)
    session.add_track("remote", "Others", "remote", 48000, 1)
    feed(session, "mic", tone(16000, 1.5, 440, click_at=0.5), 250, 16000)
    feed(session, "remote", tone(48000, 1.2, 880, click_at=0.25), 0, 48000)
    session.finish()
    return session.directory


def twin(tmp_path, compacted=("mic", "remote")):
    """(WAV meeting, compacted meeting) of the same capture, in two Libraries."""
    original = capture(tmp_path / "wav")
    copy = tmp_path / "opus" / original.name
    shutil.copytree(original, copy)
    for identifier in compacted:
        compact(copy, identifier)
    wav = LibraryScanner(tmp_path / "wav").scan().meetings[0]
    opus = LibraryScanner(tmp_path / "opus").scan().meetings[0]
    return wav, opus


def by_kind(meeting):
    return {t.kind: t for t in meeting.tracks}


def test_scanner_lists_compacted_spans_like_their_wavs(tmp_path):
    wav, opus = twin(tmp_path)
    assert [d.code for d in opus.diagnostics] == [d.code for d in wav.diagnostics]
    assert opus.has_audio and opus.lifecycle == wav.lifecycle == "saved"
    assert opus.title == wav.title and opus.started_at_ms == wav.started_at_ms
    assert abs(opus.duration_ms - wav.duration_ms) <= OPUS_FRAME_MS
    for kind, before in by_kind(wav).items():
        after = by_kind(opus)[kind]
        assert before.codec == "pcm" and before.decoded_frames is None
        assert after.codec == "opus" and after.path.parent.name == "tracks"
        # Source metadata is kept; the decoded 48 kHz length is compared as time.
        assert (after.id, after.frames, after.sample_rate, after.start_offset_ms) == (
            before.id,
            before.frames,
            before.sample_rate,
            before.start_offset_ms,
        )
        assert abs(after.decoded_frames - before.frames * 48000 / before.sample_rate) <= 960
        assert (after.pcm_rate, after.pcm_frames) == (48000, after.decoded_frames)
        assert (before.pcm_rate, before.pcm_frames) == (before.sample_rate, before.frames)


def test_mixed_session_reads_both_and_rescans_after_compaction(tmp_path):
    directory = capture(tmp_path)
    scanner = LibraryScanner(tmp_path)
    first = scanner.scan().meetings[0]
    compact(directory, "mic")
    mixed = scanner.scan().meetings[0]
    assert {t.kind: t.codec for t in mixed.tracks} == {"microphone": "opus", "remote": "pcm"}
    assert not [d for d in mixed.diagnostics if d.code == "capture-track-unreadable"]
    assert abs(mixed.duration_ms - first.duration_ms) <= OPUS_FRAME_MS
    manifest = load_session(directory / "session.json")
    assert [t.get("codec") for t in manifest["tracks"]] == ["opus", None]


def test_non_48k_source_compares_time_not_raw_frames(tmp_path):
    session = RecordingSession(tmp_path, "44.1")
    session.add_track("media", "Media", "media", 44100, 2)
    feed(session, "media", tone(44100, 1.5, 300, channels=2), 0, 44100, 2)
    session.finish()
    span = compact(session.directory, "media")
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    (track,) = meeting.tracks
    assert (track.frames, track.sample_rate, track.channels) == (66150, 44100, 2)
    # 66150 source frames are 72000 samples at 48 kHz; a raw comparison would fail.
    assert track.decoded_frames != track.frames and abs(track.decoded_frames - 72000) <= 960
    assert meeting.duration_ms == 1500

    # A rate that contradicts the decoded length is rejected, not rescaled.
    manifest = json.loads((session.directory / "session.json").read_text())
    manifest["tracks"][0]["sampleRate"] = 22050
    (session.directory / "session.json").write_text(json.dumps(manifest))
    rejected = LibraryScanner(tmp_path).scan().meetings[0]
    assert not rejected.tracks and "capture-track-unreadable" in {d.code for d in rejected.diagnostics}
    with pytest.raises(ValueError, match="track-length-mismatch"):
        load_session(session.directory / "session.json")
    assert span["frames"] == 66150


def test_length_check_is_one_opus_frame_in_48k_time():
    track_source.check_length(72000, 24000, 16000)
    track_source.check_length(72000 + 960, 24000, 16000)
    track_source.check_length(71999, 66150, 44100)
    with pytest.raises(ValueError, match="track-length-mismatch"):
        track_source.check_length(72000 + 961, 24000, 16000)
    with pytest.raises(ValueError, match="track-length-mismatch"):
        track_source.check_length(72000, 72000, 16000)


@pytest.mark.parametrize(
    "span",
    [
        dict(file="/etc/passwd", codec="opus", decodedRate=48000),
        dict(file="../tracks/mic.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/../tracks/mic.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/sub/mic.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/.mic.opus", codec="opus", decodedRate=48000),
        dict(file="tracks\\mic.opus", codec="opus", decodedRate=48000),
        dict(file="other/mic.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/mic.wav", codec="opus", decodedRate=48000),
        dict(file="tracks/mic.opus", codec="flac", decodedRate=48000),
        dict(file="tracks/mic.opus", codec="opus", decodedRate=44100),
        dict(file="tracks/mic.opus", codec="opus"),
        dict(file="tracks/mic.opus"),
        dict(file="track-mic.wav", codec=None),
        dict(file="track-mic.wav", codec="wav"),
        dict(file="track-mic.wav", codec="pcm"),
        dict(file="tracks/a b.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/a:b.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/\u00fc.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/.opus", codec="opus", decodedRate=48000),
        dict(file="tracks/" + "a" * 65 + ".opus", codec="opus", decodedRate=48000),
    ],
)
def test_unsafe_or_unknown_references_are_rejected(span):
    with pytest.raises(ValueError):
        track_source.span_reference(span)


@pytest.mark.parametrize("attack", ["file-symlink", "folder-symlink", "fifo", "directory", "truncated"])
def test_escaping_or_irregular_files_never_become_tracks(tmp_path, attack):
    library = tmp_path / "Library"
    directory = capture(library)
    compact(directory, "mic")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = directory / "tracks" / "mic.opus"
    shutil.copy(target, outside / "mic.opus")
    if attack == "file-symlink":
        target.unlink()
        target.symlink_to(outside / "mic.opus")
    elif attack == "folder-symlink":
        shutil.rmtree(directory / "tracks")
        (directory / "tracks").symlink_to(outside)
    elif attack == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif attack == "directory":
        target.unlink()
        target.mkdir()
    else:
        target.write_bytes(target.read_bytes()[:200])
    meeting = LibraryScanner(library).scan().meetings[0]
    assert [t.kind for t in meeting.tracks] == ["remote"]
    assert "capture-track-unreadable" in {d.code for d in meeting.diagnostics}
    with pytest.raises((OSError, ValueError)):
        load_session(directory / "session.json")
    if attack != "truncated":
        with pytest.raises((OSError, ValueError)):
            track_source.open_track(target, "opus")
    with pytest.raises((OSError, ValueError)):
        track_source.OpusDecoder(target)


def test_salvage_without_session_json_recovers_compacted_spans(tmp_path):
    directory = capture(tmp_path)
    compact(directory, "mic")
    compact(directory, "remote")
    (directory / "session.json").unlink()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert sorted(t.path.name for t in meeting.tracks) == ["mic.opus", "remote.opus"]
    assert all(t.codec == "opus" and t.sample_rate == 48000 for t in meeting.tracks)
    assert meeting.interrupted_at_ms is not None
    manifest = load_session(directory / "session.json")
    assert manifest["source"] == "recovered"
    assert [(t["file"], t["codec"], t["sampleRate"]) for t in manifest["tracks"]] == [
        ("tracks/mic.opus", "opus", 48000),
        ("tracks/remote.opus", "opus", 48000),
    ]
    assert all(abs(t["frames"] - 72000) <= 960 or abs(t["frames"] - 57600) <= 960 for t in manifest["tracks"])


def test_salvage_prefers_wavs_and_keeps_every_source(tmp_path):
    directory = capture(tmp_path)
    compact(directory, "mic", keep_wav=True)
    (directory / "session.json").unlink()
    before = sorted(p.relative_to(directory) for p in directory.rglob("*"))
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert {t.codec for t in meeting.tracks} == {"pcm"} and len(meeting.tracks) == 2
    assert all(t["file"].endswith(".wav") for t in load_session(directory / "session.json")["tracks"])
    assert sorted(p.relative_to(directory) for p in directory.rglob("*")) == before


def decode_all(path):
    decoder = track_source.OpusDecoder(path)
    try:
        return decoder.read(10**7), decoder.info
    finally:
        decoder.close()


def test_decoder_is_trimmed_and_seeks_sample_exactly(tmp_path):
    directory = capture(tmp_path)
    compact(directory, "mic")
    path = directory / "tracks" / "mic.opus"
    full, info = decode_all(path)
    assert len(full) // 2 == info.samples and abs(info.samples - 72000) <= 960
    decoder = track_source.OpusDecoder(path)
    try:
        for frame in (0, 1, 959, 24000, 30001, 12345, info.samples - 10, 5, 70000, 100, 60500, 13000):
            decoder.seek(frame)
            got = array.array("h", decoder.read(500))
            want = array.array("h", full[frame * 2 : (frame + 500) * 2])
            assert len(got) == len(want)
            assert max((abs(a - b) for a, b in zip(got, want, strict=True)), default=0) <= 4
        decoder.seek(info.samples + 5)
        assert decoder.read(10) == b""
    finally:
        decoder.close()


def until(predicate, timeout=10):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "GStreamer did not reach the expected state"


def play(meeting, seek=None):
    from cassini_viewer.folder_player import FolderPlayer

    sink = Gst.ElementFactory.make("appsink", None)
    sink.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1"))
    sink.set_property("emit-signals", True)
    sink.set_property("sync", False)
    chunks, errors = [], []

    def received(src):
        buffer = src.emit("pull-sample").get_buffer()
        chunks.append(buffer.extract_dup(0, buffer.get_size()))
        return Gst.FlowReturn.OK

    sink.connect("new-sample", received)
    player = FolderPlayer(audio_sink=sink)
    player.connect("error", lambda _p, message: errors.append(message))
    try:
        assert FolderPlayer.can_play(meeting)
        player.load_tracks(meeting.tracks)
        until(lambda: player._prerolled or errors)
        assert not errors
        if seek is not None:
            player.seek(seek)
            until(lambda: player._pending_seek is None or errors)
        player.play()
        until(lambda: player._at_end or errors)
        assert not errors
        return array.array("h", b"".join(chunks)), player.duration
    finally:
        player.shutdown()


def magnitude(samples, start, end, frequency, rate=48000):
    block = samples[round(start * rate) : round(end * rate)]
    total = sum(
        v * complex(math.cos(2 * math.pi * frequency * i / rate), math.sin(2 * math.pi * frequency * i / rate))
        for i, v in enumerate(block)
    )
    return 2 * abs(total) / len(block) / 32768


def peak(samples, start, end, rate=48000):
    window = samples[round(start * rate) : round(end * rate)]
    return round(start * rate) + max(range(len(window)), key=lambda i: abs(window[i]))


def test_playback_matches_the_wav_timeline(tmp_path):
    wav, opus = twin(tmp_path)
    expected, wav_duration = play(wav)
    actual, opus_duration = play(opus)
    assert abs(opus_duration - wav_duration) <= OPUS_FRAME_MS
    assert abs(len(actual) - len(expected)) <= 960
    assert magnitude(expected, 0.6, 0.7, 440) > 0.25 and magnitude(expected, 0.05, 0.2, 880) > 0.25
    for start, end, frequency in ((0.05, 0.2, 880), (0.6, 0.7, 440), (1.3, 1.6, 440)):
        assert abs(magnitude(actual, start, end, frequency) - magnitude(expected, start, end, frequency)) < 0.03
    # Click markers stay within one Opus frame: 0.25 s (remote) and 0.25 + 0.5 s (mic).
    for marker in (0.25, 0.75):
        assert abs(peak(actual, marker - 0.05, marker + 0.05) - peak(expected, marker - 0.05, marker + 0.05)) <= 960
    seeked, _ = play(opus, seek=700)
    assert abs(len(seeked) - (len(actual) - 33600)) <= 960
    assert abs(magnitude(seeked, 0.0, 0.1, 440) - magnitude(actual, 0.7, 0.8, 440)) < 0.03


def test_mixed_session_plays(tmp_path):
    wav, mixed = twin(tmp_path, compacted=("mic",))
    expected, _ = play(wav)
    actual, _ = play(mixed)
    assert abs(len(actual) - len(expected)) <= 960
    assert abs(magnitude(actual, 0.6, 0.7, 440) - magnitude(expected, 0.6, 0.7, 440)) < 0.03
    assert abs(magnitude(actual, 0.05, 0.2, 880) - magnitude(expected, 0.05, 0.2, 880)) < 0.03


def test_transcription_input_is_equivalent(tmp_path):
    np = pytest.importorskip("numpy")
    from cassini_viewer.diarization_audio import rejoin
    from cassini_viewer.speech_pipeline import audio_chunks, audio_segments
    from cassini_viewer.transcription import prepare_tracks

    wav, opus = twin(tmp_path)
    before, before_speakers = prepare_tracks(wav)
    after, after_speakers = prepare_tracks(opus)
    assert before_speakers == after_speakers
    # Old jobs are byte-identical: no codec member at all.
    assert all("codec" not in t for t in before) and all(t["codec"] == "opus" for t in after)
    for old, new in zip(before, after, strict=True):
        assert {k: old[k] for k in ("offsetMs", "speaker", "kind", "frames", "sampleRate")} == {
            k: new[k] for k in ("offsetMs", "speaker", "kind", "frames", "sampleRate")
        }
        reference = np.concatenate(list(audio_chunks(old)))
        decoded = np.concatenate(list(audio_chunks(new)))
        # Within one 20 ms Opus frame at 16 kHz.
        assert abs(len(decoded) - len(reference)) <= 320
        size = min(len(decoded), len(reference))
        assert np.corrcoef(reference[:size], decoded[:size])[0, 1] > 0.98
        segments = [dict(startSample=1600, endSample=4800), dict(startSample=8000, endSample=9600)]
        for want, got in zip(audio_segments(old, segments), audio_segments(new, segments), strict=True):
            assert len(want) == len(got) and np.corrcoef(want, got)[0, 1] > 0.98
    with rejoin(before, scratch=tmp_path / "scratch") as (_s, wav_frames, _f):
        pass
    with rejoin(after, scratch=tmp_path / "scratch") as (_s, opus_frames, _f):
        pass
    assert abs(wav_frames - opus_frames) <= 320


def export_and_decode(meeting, destination):
    from cassini_viewer.exporting import export_meeting

    exported = export_meeting(meeting, destination)
    value = portable.read_meeting(str(exported))
    assert portable.verify_audio(str(exported), value).state == portable.State.OK
    pipeline = Gst.parse_launch(
        "filesrc name=input ! oggdemux ! opusdec ! audioconvert ! "
        "audio/x-raw,format=S16LE,channels=1,rate=48000 ! appsink name=pcm sync=false"
    )
    pipeline.get_by_name("input").set_property("location", str(exported))
    pipeline.set_state(Gst.State.PLAYING)
    samples = array.array("h")
    try:
        sink = pipeline.get_by_name("pcm")
        while sample := sink.emit("try-pull-sample", 5 * Gst.SECOND):
            buffer = sample.get_buffer()
            samples.frombytes(buffer.extract_dup(0, buffer.get_size()))
    finally:
        pipeline.set_state(Gst.State.NULL)
    if sys.byteorder != "little":
        samples.byteswap()
    return value, samples


def test_cassini_export_is_equivalent(tmp_path):
    wav, opus = twin(tmp_path)
    before = {p.relative_to(opus.directory): p.read_bytes() for p in opus.directory.rglob("*") if p.is_file()}
    expected, expected_pcm = export_and_decode(wav, tmp_path / "Documents-wav")
    actual, actual_pcm = export_and_decode(opus, tmp_path / "Documents-opus")
    assert actual.manifest["audioEncode"] == expected.manifest["audioEncode"]
    assert abs(actual.manifest["audio"]["sampleCount"] - expected.manifest["audio"]["sampleCount"]) <= 960
    assert abs(actual.manifest["audio"]["durationMs"] - expected.manifest["audio"]["durationMs"]) <= OPUS_FRAME_MS
    for start, end, frequency in ((0.05, 0.2, 880), (0.6, 0.7, 440), (1.3, 1.6, 440)):
        assert abs(magnitude(actual_pcm, start, end, frequency) - magnitude(expected_pcm, start, end, frequency)) < 0.03
    after = {p.relative_to(opus.directory): p.read_bytes() for p in opus.directory.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("identifier", ["mic", "a.b-c_D9", "a..b", "-x", "a" * 64])
def test_span_ids_follow_the_track_id_rule(identifier):
    value = dict(file=f"tracks/{identifier}.opus", codec="opus", decodedRate=48000)
    assert track_source.span_reference(value) == ("opus", ("tracks", f"{identifier}.opus"))


@pytest.mark.parametrize(
    "change",
    [
        dict(codec=None),
        dict(codec="wav"),
        dict(codec="flac"),
        dict(channels=2),
        dict(file="tracks/mic phone.opus"),
    ],
)
def test_unsupported_or_mismatched_spans_are_damaged(tmp_path, change):
    directory = capture(tmp_path)
    compact(directory, "mic")
    if "file" in change:
        (directory / "tracks" / "mic.opus").rename(directory / change["file"])
    manifest = json.loads((directory / "session.json").read_text())
    manifest["tracks"][0].update(change)
    (directory / "session.json").write_text(json.dumps(manifest))
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert [t.kind for t in meeting.tracks] == ["remote"]
    assert "capture-track-unreadable" in {d.code for d in meeting.diagnostics}
    with pytest.raises((OSError, ValueError)):
        load_session(directory / "session.json")


def test_an_unknown_codec_on_a_wav_span_is_damaged_not_guessed(tmp_path):
    directory = capture(tmp_path)
    manifest = json.loads((directory / "session.json").read_text())
    manifest["tracks"][1]["codec"] = "pcm"
    (directory / "session.json").write_text(json.dumps(manifest))
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert [t.kind for t in meeting.tracks] == ["microphone"]
    with pytest.raises(ValueError):
        load_session(directory / "session.json")


def test_salvage_pairs_compacted_spans_with_their_wavs_by_source_file(tmp_path):
    directory = capture(tmp_path)
    compact(directory, "mic", keep_wav=True)  # both copies exist: the WAV wins, no duplicate
    compact(directory, "remote")  # WAV already deleted: the Opus span is used
    manifest = json.loads((directory / "session.json").read_text())
    manifest["version"] = 99  # damaged, but its span records survive
    (directory / "session.json").write_text(json.dumps(manifest))
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert "capture-recovered" in {d.code for d in meeting.diagnostics}
    assert sorted((t.codec, t.path.name.endswith(".wav")) for t in meeting.tracks) == [("opus", False), ("pcm", True)]
    assert {t.path.name for t in meeting.tracks if t.codec == "opus"} == {"remote.opus"}

    # Without any record, a WAV still present wins and the Opus span is not used.
    (directory / "session.json").unlink()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert [t.codec for t in meeting.tracks] == ["pcm"]


def test_salvage_values_rules():
    wav, opus = ["track-a.wav"], ["tracks/a.opus", "tracks/b.opus"]
    assert track_source.salvage_values(wav, opus) == [dict(file="track-a.wav")]
    assert [v["file"] for v in track_source.salvage_values([], opus)] == opus
    pairs = {"tracks/a.opus": "track-a.wav", "tracks/b.opus": "track-b.wav"}
    assert [v["file"] for v in track_source.salvage_values(wav, opus, pairs)] == ["track-a.wav", "tracks/b.opus"]
    bad = dict(tracks=[dict(file="tracks/a.opus", codec="opus", decodedRate=48000, sourceFile="../x.wav")])
    assert track_source.salvage_pairs(bad) == {} and track_source.salvage_pairs(None) == {}
