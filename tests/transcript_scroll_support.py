# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""A finished hour of synthetic English; never uses a user's transcript."""

import importlib.util
from pathlib import Path

from cassini_talk import portable, portable_writer


def hour_meeting(root):
    root.mkdir()
    source = Path(__file__).parents[1] / "ux/library-vectors/assets/build.py"
    spec = importlib.util.spec_from_file_location("scroll_opus_fixture", source)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = root
    generator.build("seed.opus", 3600000, title="Synthetic English planning meeting", portable=True)
    sentence = [
        "The",
        "internationalization",
        "implementation",
        "incorporates",
        "comprehensive",
        "instrumentation",
        "for",
        "reproducibility",
        "and",
        "responsiveness",
        "while",
        "accessibility",
        "improvements",
        "preserve",
        "understandable",
        "navigation",
        "and",
        "predictable",
        "interaction.",
    ]
    items, stamp = [], 0
    for turn in range(480):
        pattern = ((1, 2, 4, 8), (30, 40, 50, 60), (2, 5, 10, 15))[0 if turn < 120 else 1 if turn < 360 else 2]
        for word in range(pattern[turn % 4]):
            items.append(
                dict(
                    speaker=f"person-{turn % 3}", startMs=stamp, endMs=stamp + 180, text=sentence[word % len(sentence)]
                )
            )
            stamp += 200
        stamp += 700
    for word in items:
        word["startMs"] = round(word["startMs"] * 3600000 / stamp)
        word["endMs"] = round(word["endMs"] * 3600000 / stamp)
    seed = root / "seed.opus"
    meeting = portable.read_meeting(str(seed))
    manifest = meeting.manifest
    manifest["speakers"] = [dict(id=f"person-{i}", label=name) for i, name in enumerate(("Alice", "Ben", "Claire"))]
    word_tags, reference = portable_writer.chunk_set(
        "CASSINI_TX_MAIN_PAYLOAD_",
        dict(format=portable.WORDS_FORMAT, language="en", wordCount=len(items), items=items),
        portable_writer.WORDS_MIME,
    )
    manifest["transcripts"][0].update(wordCount=len(items), payloadRef=reference)
    manifest_tags, _ = portable_writer.chunk_set("CASSINI_PAYLOAD_", manifest, portable_writer.MANIFEST_MIME)
    target = root / "synthetic-hour.opus"
    with seed.open("rb") as stream, target.open("wb") as output:
        portable_writer.rewrite_tags(stream, output, dict(meeting.tags, **word_tags, **manifest_tags))
    seed.unlink()
    return target
