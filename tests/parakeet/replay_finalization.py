#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare Stop-to-publication on a paced, mixed PCM16 meeting.

Run with PYTHONPATH=src, the packaged bundle, matching Python ORT and pinned
models. Output contains timings and counts only. Both libraries are disposable.
"""

import argparse
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from queue_models import Models

from cassini_talk import speech_pipeline as speech
from cassini_talk.meeting_store import atomic_json, read_metadata
from cassini_talk.parakeet import Parakeet
from cassini_talk.parakeet_client import ParakeetClient
from cassini_talk.recording_session import RecordingSession
from cassini_talk.transcription import TranscriptionQueue
from cassini_talk.voice_store import VoiceStore
from cassini_talk.whistle_client import WhistleClient
from cassini_talk.whistle_worker import PRIVACY, verify_bundle


class GrowingTrack:
    """A writer watermark only. The recognizer reads committed frames at its own pace."""

    def __init__(self, path):
        self.path = path
        self.condition = threading.Condition()
        self.frames = 0
        self.stopped = False

    def advance(self, frames):
        with self.condition:
            self.frames = frames
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify()

    def blocks(self, rate, channels):
        cursor = 0
        while True:
            with self.condition:
                while not self.stopped and self.frames == cursor:
                    self.condition.wait()
                if self.stopped:
                    return
                count = min(rate, self.frames - cursor)
            with wave.open(str(self.path)) as wav:
                wav.setpos(cursor)
                pcm = wav.readframes(count)
            if len(pcm) != 2 * channels * count:
                raise ValueError("Committed PCM unavailable")
            yield pcm
            cursor += count


def precompute(track, args, rate, channels):
    import numpy as np

    gate = speech.SpeechGate(speech.SpeechDetector(args.bundle / "silero_vad.onnx"))
    if args.parakeet:
        model = Parakeet(args.parakeet, args.runtime)
    else:
        os.environ["NEEDLE3_LIB_PATH"] = str(args.bundle / "libneedle3.so")
        sys.path.insert(0, str(args.bundle / "wrapper.whl"))
        model = importlib.import_module("needle").Whistle(weights=args.bundle / "whistle.cact")
    completed, pieces = [], []
    try:

        def mono():
            for pcm in track.blocks(rate, channels):
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
                if channels == 2:
                    samples = samples.reshape(-1, 2).mean(axis=1, dtype=np.float32)
                yield samples / np.float32(32768)

        samples_in = speech.resampled_stream(mono()) if rate == 48000 else mono()
        for block in samples_in:
            for chunk in gate.feed(block):
                if not pieces:
                    start = chunk.start_sample
                pieces.append(chunk.samples)
                if chunk.end:
                    samples = np.concatenate(pieces)
                    segment = dict(startSample=start, endSample=start + len(samples), reason=chunk.reason)
                    value, words, lang, digest = speech.recognise_segment(model, None, segment, samples=samples)
                    completed.append((segment, value["text"], words, lang, digest, time.monotonic()))
                    pieces.clear()
    finally:
        if args.parakeet:
            model.close()
    return completed


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
    session = RecordingSession(library, "Replay", source="gnome")
    growing = GrowingTrack(session.directory / "track-system.wav")
    with wave.open(str(args.audio)) as wav, ThreadPoolExecutor(max_workers=1) as pool:
        rate, channels = wav.getframerate(), wav.getnchannels()
        if rate not in (16000, 48000) or channels not in (1, 2) or wav.getsampwidth() != 2:
            raise ValueError("Supply a mono or stereo 16/48 kHz PCM16 WAV")
        session.add_track("system", None, "system", rate, channels, filename="track-system.wav")
        started = time.monotonic()
        worker = pool.submit(precompute, growing, args, rate, channels) if live else None
        try:
            while pcm := wav.readframes(rate // 4):
                if live:
                    time.sleep(max(0, started + wav.tell() / rate - time.monotonic()))
                session.append("system", pcm, (wav.tell() - len(pcm) // (2 * channels)) * 1000 / rate)
                growing.advance(wav.tell())
        finally:
            stop = time.monotonic()
            growing.stop()
            session.finish()
        completed = worker.result() if worker else []
    marks, misses = {}, 0

    class Client(ParakeetClient if args.parakeet else WhistleClient):
        def process_tracks(self, tracks, progress):
            nonlocal misses
            (track,) = tracks
            entries = cache_entries(track, completed, stop)
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
        groups = json.loads(turns)["groups"]
        if not groups or not any(g["turns"] for g in groups):
            raise RuntimeError("No diarization turns to compare")
        report = dict(
            secondsAfterStop=round(published - stop, 3),
            stopThroughPlanning=round(marks["planned"] - stop, 3),
            recognition=round(marks["recognised"] - marks["planned"], 3),
            diarizationThroughFirstTurnBatch=round(marks["diarized"] - marks["recognised"], 3),
            remainingPublication=round(published - marks["diarized"], 3),
            postStopCacheMisses=misses,
            completedBeforeStop=sum(row[-1] <= stop for row in completed),
            words=len(json.loads(words)["items"]),
            speakers=len({w["speaker"] for w in json.loads(words)["items"]}),
            turns=sum(len(group["turns"]) for group in groups),
        )
        if voice_store:
            prints = voice_store.meeting(session.directory)["speakers"]
            if not prints or "voiceEnd" not in marks:
                raise RuntimeError("Voice extraction did not complete")
            report["voicePrints"] = len(prints)
            report["voiceExtraction"] = round(marks["voiceEnd"] - marks["voiceStart"], 3)
            report["remainingPublication"] = round(report["remainingPublication"] - report["voiceExtraction"], 3)
        return report, words, turns
    finally:
        jobs.close()
        jobs.executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("audio", "bundle", "runtime", "nemotron"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--parakeet", type=Path)
    parser.add_argument("--campplus", type=Path)
    args = parser.parse_args()
    os.environ.update(PRIVACY)
    verify_bundle(args.bundle)
    with tempfile.TemporaryDirectory() as root:
        batch, words, turns = run(args, Path(root) / "batch", False)
        live, other_words, other_turns = run(args, Path(root) / "live", True)
    print(
        json.dumps(
            dict(batch=batch, reuse=live, identicalWords=words == other_words, identicalTurns=turns == other_turns),
            indent=2,
        )
    )
    if words != other_words or turns != other_turns or live["postStopCacheMisses"] >= batch["postStopCacheMisses"]:
        raise SystemExit("Replay failed to reproduce final output with fewer post-Stop decodes")


if __name__ == "__main__":
    main()
