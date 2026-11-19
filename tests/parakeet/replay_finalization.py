#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare Stop-to-publication on a paced, mixed PCM16 meeting.

Run with PYTHONPATH=src, the packaged bundle, matching Python ORT and pinned
models and GStreamer Opus plugins. Both managed libraries are disposable.
The report contains timings, counts and equality checks. Use --reuse-first to
measure order effects and --microphone for a second, simultaneously recorded track.
"""

import argparse
import importlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import ExitStack, closing, suppress
from pathlib import Path

from queue_models import Models

from cassini_talk import portable
from cassini_talk import speech_pipeline as speech
from cassini_talk.library_publication import capture_parent, publish_stopped
from cassini_talk.meeting_store import atomic_json, read_metadata
from cassini_talk.parakeet import Parakeet
from cassini_talk.parakeet_client import ParakeetClient
from cassini_talk.recording_session import RecordingSession
from cassini_talk.speech_threads import background_worker_priority
from cassini_talk.transcription import TranscriptionQueue
from cassini_talk.voice_store import VoiceStore
from cassini_talk.whistle_client import WhistleClient
from cassini_talk.whistle_worker import PRIVACY, verify_bundle

WATERMARK = struct.Struct("<Q")


def watermarks():
    """Each atomic writer frame count supersedes earlier ones."""
    while packet := sys.stdin.buffer.read1(WATERMARK.size * 128):
        if len(packet) % WATERMARK.size:
            raise ValueError("Truncated watermark")
        yield WATERMARK.unpack_from(packet, len(packet) - WATERMARK.size)[0]


def committed_pcm(path):
    cursor = 0
    for frames in watermarks():
        with wave.open(str(path)) as wav:
            wav.setpos(cursor)
            while cursor < frames:
                count = min(wav.getframerate(), frames - cursor)
                pcm = wav.readframes(count)
                if len(pcm) != 2 * wav.getnchannels() * count:
                    raise ValueError("Committed PCM unavailable")
                yield pcm
                cursor += count


def fixed_clock_interval(path, rate, channels):
    """Replay the first permanently anchored minute through the batch clock mapper."""
    processed = False
    for frames in watermarks():
        if processed or frames <= rate * 60:
            continue
        manifest = json.loads((path.parent / "session.json").read_text())
        track = next(t for t in manifest["tracks"] if t["file"] == path.name)
        anchors = track.get("clockAnchors", [])
        # The last point can still move. The preceding ones are fixed.
        if len(anchors) < 3 or anchors[0][0] != 0:
            continue
        first, second = anchors[:2]
        length = second[0] + rate // 4
        if frames < length:
            continue
        # One bounded snapshot supplies real right context to the batch resampler.
        # Keep it under the probe's disposable library even on cancellation.
        with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
            snapshot = Path(scratch) / path.name
            with wave.open(str(path)) as source, wave.open(str(snapshot), "wb") as output:
                output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
                remaining = length
                while remaining:
                    count = min(remaining, rate)
                    pcm = source.readframes(count)
                    if len(pcm) != count * channels * 2:
                        raise ValueError("Incomplete fixed interval")
                    output.writeframesraw(pcm)
                    remaining -= count
            mapped = dict(
                id="system",
                path=str(snapshot),
                frames=length,
                sampleRate=rate,
                channels=channels,
                offsetMs=0,
                clockAnchors=[first, second],
            )
            # Leave the FIR right halo and one linear-interpolation neighbour.
            halo = (len(speech.TAPS) // 2 + 2) // 3 + 1
            available = (second[1] - first[1]) * speech.RATE // 1_000_000_000 - halo
            with closing(speech.audio_chunks(mapped)) as blocks:
                for block in blocks:
                    if available <= 0:
                        break
                    yield block[:available]
                    available -= len(block)
        processed = True


def precompute(args, path):
    import numpy as np

    background_worker_priority()
    with wave.open(str(path)) as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
    gate = speech.SpeechGate(speech.SpeechDetector(args.bundle / "silero_vad.onnx"))
    if args.parakeet:
        model = Parakeet(args.parakeet, args.runtime)
    else:
        os.environ["NEEDLE3_LIB_PATH"] = str(args.bundle / "libneedle3.so")
        sys.path.insert(0, str(args.bundle / "wrapper.whl"))
        model = importlib.import_module("needle").Whistle(weights=args.bundle / "whistle.cact")
    pieces = []
    try:

        def mono():
            for pcm in committed_pcm(path):
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
                if channels == 2:
                    samples = samples.reshape(-1, 2).mean(axis=1, dtype=np.float32)
                yield samples / np.float32(32768)

        samples_in = speech.resampled_stream(mono()) if rate == 48000 else mono()
        if args.fixed_clock_interval:
            samples_in = fixed_clock_interval(path, rate, channels)
        for block in samples_in:
            for chunk in gate.feed(block):
                if not pieces:
                    start = chunk.start_sample
                pieces.append(chunk.samples)
                if chunk.end:
                    samples = np.concatenate(pieces)
                    segment = dict(startSample=start, endSample=start + len(samples), reason=chunk.reason)
                    value, words, lang, digest = speech.recognise_segment(model, None, segment, samples=samples)
                    print(json.dumps([segment, value["text"], words, lang, digest, time.monotonic()]), flush=True)
                    pieces.clear()
    finally:
        if args.parakeet:
            model.close()


def cache_entries(track, completed, stop):
    # The probe creates one continuous system track at offset zero. The final
    # worker independently replans it and checks every key against saved PCM.
    if track["offsetMs"] != 0:
        raise ValueError("Probe requires zero track offset")
    presence, end = track.get("sessionPresence"), speech._source_end(track)
    entries = {}
    for segment, text, words, lang, digest, finished in completed:
        if finished > stop or end is not None and words and max(w["endMs"] for w in words) > end:
            continue
        kept = speech.present_words(words, presence)
        key = speech._segment_cache_key(digest, track["offsetMs"], segment, end, presence)
        entries[key] = dict(
            text=text if kept == words else " ".join(w["text"] for w in kept), words=kept, language=lang
        )
    return entries


def run(args, library, live):
    library.mkdir()
    os.environ.update(
        CASSINI_LIBRARY_DIR=str(library),
        XDG_CONFIG_HOME=str(library.parent / (library.name + "-config")),
        XDG_DATA_HOME=str(library.parent / (library.name + "-data")),
    )
    session = RecordingSession(capture_parent(library), "Replay", source="gnome")
    max_capture_lag = 0.0
    with ExitStack() as stack:
        tracks = {}
        for kind, path in (("system", args.audio), ("microphone", args.microphone)):
            if path is None:
                continue
            wav = stack.enter_context(wave.open(str(path)))
            rate, channels = wav.getframerate(), wav.getnchannels()
            if rate not in (16000, 48000) or channels not in (1, 2) or wav.getsampwidth() != 2:
                raise ValueError("Supply a mono or stereo 16/48 kHz PCM16 WAV")
            session.add_track(kind, None, kind, rate, channels, filename=f"track-{kind}.wav")
            tracks[kind] = wav
        system = tracks["system"]
        duration = max(w.getnframes() / w.getframerate() for w in tracks.values())
        env = WhistleClient(args.bundle).worker_environment() if live else None
        started = time.monotonic()
        worker = None
        if live:
            output = stack.enter_context(tempfile.TemporaryFile())
            worker = stack.enter_context(
                subprocess.Popen(
                    [sys.executable, __file__, *sys.argv[1:], "--follow", str(session.directory / "track-system.wav")],
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=output,
                )
            )
            os.set_blocking(worker.stdin.fileno(), False)
        try:
            while any(w.tell() < w.getnframes() for w in tracks.values()):
                blocks = []
                for kind, wav in tracks.items():
                    first, rate = wav.tell(), wav.getframerate()
                    pcm = wav.readframes(rate // 4)
                    if pcm:
                        blocks.append((kind, pcm, first * 1000 / rate, wav.tell() / rate))
                if not blocks:
                    raise ValueError("Truncated input WAV")
                deadline = started + max(end for _, _, _, end in blocks)
                if live:
                    time.sleep(max(0, deadline - time.monotonic()))
                for kind, pcm, offset, _ in blocks:
                    if args.native_drift_ppm is None:
                        session.append(kind, pcm, offset)
                    else:
                        # Both lanes observe the same deterministic device clock. Its drift
                        # makes the finalizer resample raw PCM onto the session timeline.
                        host_ns = session._native_clock_origin_ns + round(offset * (1_000_000 + args.native_drift_ppm))
                        session.append_native(kind, pcm, offset, host_ns=host_ns)
                if worker:
                    if worker.poll() is not None:
                        raise RuntimeError(f"Capture recognizer exited with code {worker.returncode}")
                    # The next watermark covers the same frames on disk.
                    with suppress(BlockingIOError):
                        os.write(worker.stdin.fileno(), WATERMARK.pack(system.tell()))
                max_capture_lag = max(max_capture_lag, time.monotonic() - deadline)
        finally:
            stop = time.monotonic()
            if worker:
                worker.kill()
                worker.wait()
            session.finish()
        audio_file = publish_stopped(session)
        audio_published = time.monotonic()
        completed = []
        if worker:
            output.seek(0)
            # Stop can interrupt the final line. Only complete, flushed results qualify.
            completed = [json.loads(line) for line in output if line.endswith(b"\n")]
    marks, misses = {}, 0

    class Client(ParakeetClient if args.parakeet else WhistleClient):
        def process_tracks(self, tracks, progress):
            nonlocal misses
            system_track = next(track for track in tracks if track["kind"] == "system")
            entries = cache_entries(system_track, completed, stop)
            if entries:
                opts = self.recognition_cache
                atomic_json(
                    Path(opts["path"]),
                    dict(format=speech.RECOGNITION_CACHE_FORMAT, model=opts["model"], segments=entries),
                )

            def observed(event):
                nonlocal misses
                misses += "cacheEntry" in event
                marks.setdefault(event["type"], time.monotonic())
                progress(event)

            return super().process_tracks(tracks, observed)

        def voiceprints(self, *args, **kwargs):
            marks["voiceStart"] = time.monotonic()
            result = super().voiceprints(*args, **kwargs)
            marks["voiceEnd"] = time.monotonic()
            return result

    class PinnedModel:
        def __init__(self, path):
            self.path = path

        def request(self, _directory, *, origin):
            return dict(modelPath=str(self.path), runtimePath=str(args.runtime), origin=origin)

        def measured(self, _factor):
            pass

    done = threading.Event()
    voice_store = VoiceStore(library.parent / (library.name + "-voices")) if args.campplus else None
    jobs = TranscriptionQueue(
        lambda _: None,
        lambda _: done.set(),
        bundle=args.bundle,
        root=library,
        models=Models(args.parakeet, args.runtime, client_factory=Client) if args.parakeet else None,
        client_factory=Client,
        diarization_models=PinnedModel(args.nemotron),
        voice_store=voice_store,
        voice_models=PinnedModel(args.campplus) if args.campplus else None,
    )
    try:
        if not jobs.enqueue(session.directory) or not done.wait(900):
            raise RuntimeError("Final queue did not finish")
        published = time.monotonic()
        if read_metadata(session.directory)["transcript"]["status"] != "done":
            raise RuntimeError("Final queue failed")
        words, turns = [(session.directory / name).read_bytes() for name in ("transcript.json", "speaker-turns.json")]
        embedded = portable.read_meeting(str(library / audio_file.name))
        file_words = portable.load_words(embedded, embedded.default_transcript_id)
        if not file_words:
            raise RuntimeError("Published recording contains no default transcript")
        groups = json.loads(turns)["groups"]
        if not groups or not any(g["turns"] for g in groups):
            raise RuntimeError("No diarization turns to compare")
        report = dict(
            audioSeconds=duration,
            tracks=len(tracks),
            maxWriterLagMs=round(max_capture_lag * 1000, 1) if live else None,
            secondsAfterStop=round(published - stop, 3),
            audioPublication=round(audio_published - stop, 3),
            setupAndPlanning=round(marks["planned"] - audio_published, 3),
            recognition=round(marks["recognised"] - marks["planned"], 3),
            diarizationThroughFirstTurnBatch=round(marks["diarized"] - marks["recognised"], 3),
            remainingPublication=round(published - marks["diarized"], 3),
            postStopCacheMisses=misses,
            completedBeforeStop=sum(row[-1] <= stop for row in completed),
            words=len(json.loads(words)["items"]),
            speakers=len({w["speaker"] for w in json.loads(words)["items"]}),
            turns=sum(len(group["turns"]) for group in groups),
        )
        if not report["words"]:
            raise RuntimeError("Empty transcript")
        if voice_store:
            prints = voice_store.meeting(session.directory)["speakers"]
            if not prints or "voiceEnd" not in marks:
                raise RuntimeError("Voice extraction did not complete")
            if not marks["diarized"] <= marks["voiceStart"] <= marks["voiceEnd"] <= published:
                raise RuntimeError("Voice extraction overlapped the reported stages")
            report["voicePrints"] = len(prints)
            report["voiceExtraction"] = round(marks["voiceEnd"] - marks["voiceStart"], 3)
            report["remainingPublication"] = round(report["remainingPublication"] - report["voiceExtraction"], 3)
        return report, words, turns, (file_words, embedded.speakers), embedded.manifest["integrity"]["opusAudioSha256"]
    finally:
        jobs.close()
        jobs.executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("audio", "bundle", "runtime", "nemotron"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--parakeet", type=Path)
    parser.add_argument("--campplus", type=Path)
    parser.add_argument(
        "--native-drift-ppm",
        type=int,
        choices=range(-1000, 1001),
        metavar="PPM",
        help="Exercise native capture with a synthetic sample clock offset in parts per million",
    )
    parser.add_argument(
        "--fixed-clock-interval", action="store_true", help="Decode the first fixed native clock interval"
    )
    parser.add_argument("--microphone", type=Path, help="Record a simultaneous mic track and finalize it at Stop")
    parser.add_argument("--reuse-first", action="store_true", help="Reverse order to expose warm-cache effects")
    parser.add_argument("--follow", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixed_clock_interval and args.native_drift_ppm is None:
        parser.error("--fixed-clock-interval requires --native-drift-ppm")
    os.environ.update(PRIVACY)
    verify_bundle(args.bundle)
    if args.follow:
        from cassini_talk.whistle_worker import block_network

        sys.addaudithook(block_network)
        precompute(args, args.follow)
        return
    with tempfile.TemporaryDirectory() as root:
        results = {}
        for use_live in (args.reuse_first, not args.reuse_first):
            results[use_live] = run(args, Path(root) / ("live" if use_live else "batch"), use_live)
        batch, words, turns, embedded, audio_digest = results[False]
        live, other_words, other_turns, other_embedded, other_digest = results[True]
    print(
        json.dumps(
            dict(
                batch=batch,
                reuse=live,
                identicalWords=words == other_words,
                identicalTurns=turns == other_turns,
                identicalPublishedTranscript=embedded == other_embedded,
                identicalPublishedAudio=audio_digest == other_digest,
            ),
            indent=2,
        )
    )
    if audio_digest != other_digest:
        raise SystemExit("Published Opus payload digest differs")
    if embedded != other_embedded or words != other_words or turns != other_turns:
        raise SystemExit("Final words, speakers or turns differ")
    if (args.fixed_clock_interval or not args.native_drift_ppm) and live["postStopCacheMisses"] >= batch[
        "postStopCacheMisses"
    ]:
        raise SystemExit("No reduction in post-Stop decodes")


if __name__ == "__main__":
    main()
