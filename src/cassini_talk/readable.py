# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable readable blocks and timing repair; no IO or UI dependencies.

Port of gocassini 1111155, portable.ts and core/overlap.ts. Word indices
always address the input: neither grouping nor display repair edits the source.
"""

import re
from dataclasses import replace
from statistics import median

_SENTENCE = re.compile(r"""[.!?]["')\]]*$""")
_FINAL = re.compile(r"""[.?!,;:…。．！？；：，、।॥؛؟][)\]}"'’”»›」』〉》]*$""")


def readable_blocks(words, gaps, gap_break):
    """Group in file order, then merge small neighbors (portable.ts)."""
    grouped = []
    current = []
    for index, word in enumerate(words):
        if current:
            first, last = words[current[0]], words[current[-1]]
            gap = max(0, word.start_ms - last.end_ms)
            target = len(current) >= 96 or last.end_ms - first.start_ms >= 45000
            boundary = bool(_SENTENCE.search(last.text.strip())) or gap >= 2200
            if (
                word.speaker != first.speaker
                or gap_break(gaps, last, word)
                or gap > 4200
                or word.end_ms - first.start_ms > 90000
                or len(current) + 1 > 140
                or target
                and boundary
            ):
                grouped.append(current)
                current = []
        current.append(index)
    if current:
        grouped.append(current)
    merged = []
    for block in grouped:
        if merged:
            previous = merged[-1]
            a, b = words[previous[0]], words[previous[-1]]
            c, d = words[block[0]], words[block[-1]]
            small = (
                len(previous) < 18 or len(block) < 18 or b.end_ms - a.start_ms < 8000 or d.end_ms - c.start_ms < 8000
            )
            if (
                a.speaker == c.speaker
                and not gap_break(gaps, b, c)
                and max(0, c.start_ms - b.end_ms) <= 4200
                and len(previous) + len(block) <= 140
                and d.end_ms - a.start_ms <= 90000
                and small
            ):
                previous.extend(block)
                continue
        merged.append(block)
    return merged


def _candidates(words, indices):
    candidates = []
    position = 0
    while position < len(indices):
        index = indices[position]
        if not any(c.isalnum() for c in words[index].text):
            position += 1
            continue
        end = position + 1
        while end < len(indices) and not any(c.isalnum() for c in words[indices[end]].text):
            end += 1
        run = indices[position:end]
        if _FINAL.search("".join(words[i].text for i in run).rstrip()):
            candidates.append(run)
        position = end
    return candidates


def repair_word_ends(words, blocks):
    """Clip inflated terminal-word runs with speaker/meeting median budgets.

    Candidates and punctuation are excluded from the reference population.
    Fewer than eight reference words falls back to a 250 ms median. The
    provenance flag is carried on decoded words and skips only this repair.
    """
    if not words or all(word.ends_bounded_by_audio for word in words):
        return list(words)
    candidates = [run for block in blocks for run in _candidates(words, block)]
    excluded = {index for run in candidates for index in run}
    durations = {}
    for index, word in enumerate(words):
        samples = durations.setdefault(word.speaker, [])
        if index not in excluded and word.end_ms > word.start_ms and any(c.isalnum() for c in word.text):
            samples.append(word.end_ms - word.start_ms)
    population = [duration for samples in durations.values() for duration in samples]
    fallback = median(population) if len(population) >= 8 else 250
    budgets = {
        speaker: max(1000, 4 * (median(samples) if len(samples) >= 8 else fallback))
        for speaker, samples in durations.items()
    }
    repaired = list(words)
    for run in candidates:
        start = min(words[i].start_ms for i in run)
        end = max(words[i].end_ms for i in run)
        budget = int(budgets[words[run[0]].speaker])
        if end - start > budget:
            for i in run:
                word = words[i]
                repaired[i] = replace(word, end_ms=max(word.start_ms, min(word.end_ms, start + budget)))
    return repaired
