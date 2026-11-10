#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate portable, silent synthetic English fixtures for all renderer spikes."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from cassini_talk import portable, portable_writer  # noqa: E402


def generate(directory, *, minutes=60, named=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "synthetic.opus"
    count = 490 if minutes == 60 else round(2000 * minutes / 240)
    sentence = "The engineering team reviewed the internationalization implementation and discussed comprehensive instrumentation for reproducibility accessibility and predictable interaction.".split()
    pattern = (1, 2, 4, 6, 20, 35, 3, 1, 65, 4, 115, 44)
    words, stamp = [], 0
    for turn in range(count):
        speaker = f"person-{turn % 2}" if named else "speaker-unassigned"
        n = pattern[turn % len(pattern)]
        for index in range(n):
            words.append(dict(speaker=speaker, startMs=stamp, endMs=stamp + 180,
                              text=f"{turn + 1}." if index == 0 else sentence[index % len(sentence)]))
            if named and n >= 35 and index == n // 2:
                for delta, value in ((20, "Sounds"), (170, "good.")):
                    words.append(dict(speaker=f"person-{1-turn%2}", startMs=stamp + delta,
                                      endMs=stamp + delta + 150, text=value))
            stamp += 200
        if named and turn % 24 == 10:
            for i in range(18):
                words.append(dict(speaker=f"person-{1-turn%2}", startMs=stamp - 1500 + i*200,
                                  endMs=stamp - 1320 + i*200, text=sentence[i % len(sentence)]))
            stamp += 2400
        stamp += 700
    words.sort(key=lambda word: word["startMs"])
    for word in words:
        for key in ("startMs", "endMs"):
            word[key] = round(word[key] * minutes * 60000 / stamp)
    spec = importlib.util.spec_from_file_location("render_fixture_seed", ROOT / "ux/library-vectors/assets/build.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    seed.ROOT = directory
    seed.build("seed.opus", minutes*60000, title=f"Synthetic English {minutes}-minute meeting", portable=True)
    original = portable.read_meeting(str(directory / "seed.opus"))
    manifest = original.manifest
    manifest["speakers"] = [dict(id=f"person-{i}", label=name) for i,name in enumerate(("Alice", "Ben"))] if named else []
    payload, reference = portable_writer.chunk_set("CASSINI_TX_MAIN_PAYLOAD_",
        dict(format=portable.WORDS_FORMAT, language="en", wordCount=len(words), items=words), portable_writer.WORDS_MIME)
    manifest["transcripts"][0].update(wordCount=len(words), payloadRef=reference)
    tags, _ = portable_writer.chunk_set("CASSINI_PAYLOAD_", manifest, portable_writer.MANIFEST_MIME)
    with (directory/"seed.opus").open("rb") as source, target.open("wb") as output:
        portable_writer.rewrite_tags(source, output, dict(original.tags, **payload, **tags))
    (directory/"seed.opus").unlink()
    receipt = dict(minutes=minutes, sourceTurns=count, words=len(words), namedSpeakers=2 if named else 0,
                   path=str(target), note="Silent synthesized fixture; no real audio/transcript. Unassigned uses one required placeholder word speaker id and an empty named roster.")
    (directory/"fixture.json").write_text(json.dumps(receipt, indent=2)+"\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--minutes", type=int, choices=(60,240), default=60)
    parser.add_argument("--unassigned", action="store_true")
    args = parser.parse_args()
    print(json.dumps(generate(args.directory, minutes=args.minutes, named=not args.unassigned)))
