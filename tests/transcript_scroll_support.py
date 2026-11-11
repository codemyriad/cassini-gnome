# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""A finished hour of synthetic English; never uses a user's transcript."""

import importlib.util
from pathlib import Path

from cassini_talk import portable, portable_writer


def hour_meeting(root, *, mixed=False, hours=1):
    root.mkdir()
    source = Path(__file__).parents[1] / "ux/library-vectors/assets/build.py"
    spec = importlib.util.spec_from_file_location("scroll_opus_fixture", source)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = root
    generator.build("seed.opus", 3600000 * hours, title="Synthetic English planning meeting", portable=True)
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
    for serial in range((490 if mixed else 480) * hours):
        turn = serial % (490 if mixed else 480)
        pattern = ((1, 2, 4, 8), (30, 40, 50, 60), (2, 5, 10, 15))[0 if turn < 120 else 1 if turn < 360 else 2]
        count = (
            ((1, 2, 1, 8, 3, 80, 1, 4) if turn < 120 or turn >= 360 else (1, 45, 80, 3, 120, 60, 100, 2))[turn % 8]
            if mixed
            else pattern[turn % 4]
        )
        if mixed:
            count = max(1, round(count * 0.78))
        speaker = f"person-{turn % (2 if mixed else 3)}"
        for word in range(count):
            items.append(
                dict(
                    speaker=speaker,
                    startMs=stamp,
                    endMs=stamp + 180,
                    text=f"{turn + 1}." if mixed and word == 0 else sentence[word % len(sentence)],
                )
            )
            if mixed and count >= 45 and word == count // 2:
                for offset, value in ((20, "Sounds"), (170, "good.")):
                    items.append(
                        dict(
                            speaker=f"person-{1 - turn % 2}",
                            startMs=stamp + offset,
                            endMs=stamp + offset + 150,
                            text=value,
                        )
                    )
            stamp += 200
        if mixed and turn % 20 == 9:
            for i in range(18):
                items.append(
                    dict(
                        speaker=f"person-{1 - turn % 2}",
                        startMs=stamp - 1500 + i * 200,
                        endMs=stamp - 1320 + i * 200,
                        text=sentence[i % len(sentence)],
                    )
                )
            stamp += 2400
        stamp += 700
    if mixed:
        items.sort(key=lambda word: word["startMs"])
    for word in items:
        word["startMs"] = round(word["startMs"] * 3600000 * hours / stamp)
        word["endMs"] = round(word["endMs"] * 3600000 * hours / stamp)
    seed = root / "seed.opus"
    meeting = portable.read_meeting(str(seed))
    manifest = meeting.manifest
    manifest["speakers"] = [
        dict(id=f"person-{i}", label=name)
        for i, name in enumerate(("Alice", "Ben") if mixed else ("Alice", "Ben", "Claire"))
    ]
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
