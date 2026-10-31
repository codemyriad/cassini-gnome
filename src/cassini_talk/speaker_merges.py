# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 ordered merge overlays. Original transcript items are never rewritten."""

from dataclasses import replace


def resolve_merges(entries, known):
    known = set(known)
    links = {}

    def final(identifier):
        while identifier in links:
            identifier = links[identifier]
        return identifier

    if not isinstance(entries, list):
        return links
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source, target = entry.get("from"), entry.get("into")
        if not isinstance(source, str) or not isinstance(target, str) or source not in known or target not in known:
            continue
        target = final(target)
        if source == target:
            continue  # Self edges and edges that would close a cycle are ignored.
        links[source] = target
    return {source: final(source) for source in links}


def apply_merges(words, entries, known=()):
    mapping = resolve_merges(entries, set(known) | {word.speaker for word in words})
    if not mapping:
        return words
    return tuple(replace(word, speaker=mapping.get(word.speaker, word.speaker)) for word in words)


def speaker_metrics(words):
    """One scan-side pass after sorting, independent of UI paragraph limits."""
    result, previous, current = {}, None, None
    for _index, word in sorted(enumerate(words), key=lambda item: (item[1].start_ms, item[0])):
        entry = result.setdefault(word.speaker, dict(talkMs=0, last=None, sample=None))
        start, end = word.start_ms, word.end_ms
        if end > start:
            last = entry["last"]
            if last and start <= last[1]:
                entry["talkMs"] += max(0, end - last[1])
                entry["last"] = (last[0], max(last[1], end))
            else:
                entry["talkMs"] += end - start
                entry["last"] = (start, end)
        if previous == word.speaker:
            current[1] = max(current[1], end)
        else:
            current = [start, end]
        chosen = entry["sample"]
        if end > start and (chosen is None or current[1] - current[0] > chosen[1] - chosen[0]):
            entry["sample"] = tuple(current)
        previous = word.speaker
    for entry in result.values():
        entry.pop("last")
        if entry["sample"]:
            start, end = entry["sample"]
            entry["sample"] = start, min(end, start + 10000)
    return result
