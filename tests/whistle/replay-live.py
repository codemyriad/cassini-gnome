#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pace WAVs together on the capture clock and report live vs. saved-audio accuracy."""

import argparse
import json
import re
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from queue import Empty

from cassini_talk.live_transcription import CapturedPCM, DraftTranscript, LiveAudio, LiveTranscription
from cassini_talk.parakeet_client import ParakeetClient
from cassini_talk.whistle_client import WhistleClient


def replay(paths, feed, started):
    with ExitStack() as stack:
        streams = [stack.enter_context(wave.open(str(path), "rb")) for path in paths]
        while any(wav.tell() < wav.getnframes() for wav in streams):
            blocks = []
            for source, wav in enumerate(streams):
                rate, channels, first = wav.getframerate(), wav.getnchannels(), wav.tell()
                if wav.getsampwidth() != 2 or rate not in (16000, 48000) or channels not in (1, 2):
                    raise ValueError("Replay expects 16 or 48 kHz PCM16 WAVs")
                pcm = wav.readframes(rate // 10)
                if pcm:
                    blocks.append(CapturedPCM(str(source), 0, first, rate, channels, pcm))
            deadline = started + max((block.frame + block.frames) / block.rate for block in blocks)
            time.sleep(max(0, deadline - time.monotonic()))
            for block in blocks:
                feed.offer(block)


def tokens(value):
    return re.findall(r"\w+(?:['’]\w+)*", value.casefold())


def error_rate(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, 1):
        row = [index]
        for column, actual in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[column] + 1, previous[column - 1] + (expected != actual)))
        previous = row
    return previous[-1] / len(reference) if reference else None


def measure(client, paths, references, args):
    audio, draft = LiveAudio(args.capacity), DraftTranscript()
    live = LiveTranscription(client, interval=args.interval)
    first_ms = first_settled_ms = None
    revisions = 0

    def apply(update):
        nonlocal first_ms, first_settled_ms, revisions
        prior, _ = draft.snapshot().get(update.source, ((), 0))
        old = [word.text for word in prior if update.replace_from_ms <= word.midpoint_ms <= update.through_ms]
        new = [word.text for word in update.words]
        revisions += bool(old and old != new[: len(old)])
        if first_ms is None and new:
            first_ms = round((time.monotonic() - started) * 1000)
        if first_settled_ms is None and any(word.midpoint_ms < update.settled_through_ms for word in update.words):
            first_settled_ms = round((time.monotonic() - started) * 1000)
        draft.apply(update)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        producer = executor.submit(replay, paths, audio, started)
        while not producer.done():
            try:
                block = audio.get(timeout=0.05)
            except Empty:
                continue
            for update in live.accept(block):
                apply(update)
        producer.result()
    result = live.drain(audio, saved_tracks={str(index): path for index, path in enumerate(paths)})
    for update in result.updates:
        apply(update)
    elapsed = time.monotonic() - started
    report = dict(
        firstWordsMs=first_ms,
        firstSettledWordsMs=first_settled_ms,
        tailRevisions=revisions,
        droppedBlocks=audio.dropped_blocks,
        needsSavedAudio=result.needs_saved_audio,
        seconds=round(elapsed, 3),
    )
    if references:
        source_words = draft.snapshot()
        report["liveWordErrorRate"] = [
            error_rate(reference, tokens(" ".join(word.text for word in source_words.get(str(index), ((), 0))[0])))
            for index, reference in enumerate(references)
        ]
        batch = {str(i): [] for i in range(len(paths))}

        def collect(event):
            if event["type"] == "progress" and "words" in event:
                batch[event["trackId"]].extend(event["words"])

        client.process_tracks(
            [dict(id=str(i), path=str(path), startOffsetMs=0) for i, path in enumerate(paths)], collect
        )
        report["batchWordErrorRate"] = [
            error_rate(reference, tokens(" ".join(word["text"] for word in batch[str(index)])))
            for index, reference in enumerate(references)
        ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--interval", default=1.5, type=float)
    parser.add_argument("--capacity", default=32, type=int)
    parser.add_argument("--reference", action="append", type=Path)
    parser.add_argument("wav", nargs="+", type=Path)
    args = parser.parse_args()
    if bool(args.model) != bool(args.runtime):
        parser.error("--model and --runtime are required together for Parakeet")
    if args.reference and len(args.reference) != len(args.wav):
        parser.error("provide one --reference per WAV")
    references = [tokens(path.read_text()) for path in args.reference or ()]
    paths = [path.resolve() for path in args.wav]
    clients = [WhistleClient(args.bundle)]
    if args.model:
        clients.append(ParakeetClient(args.bundle, args.model, args.runtime))
    for client in clients:
        with client:
            report = measure(client, paths, references, args)
        print(json.dumps(dict(engine=client.worker_name, **report)), flush=True)


if __name__ == "__main__":
    main()
