# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 draft R64: do not sum conflicting spans of one browser source."""

from fractions import Fraction


def bounds(track):
    """Exact half-open physical extent; display's unknown-as-zero is not alignment."""
    offset = track.get("offsetMs", track.get("startOffsetMs"))
    if offset is None:
        return None
    start = Fraction(str(offset))
    from .mapped_audio import required

    if required(track):
        from .library_v2.mixdown import RATE, total_samples
        from .mapped_audio import timeline

        mapped, _offset = timeline(track)
        # Do not floor to integer milliseconds before testing for overlap.
        end = Fraction(total_samples(mapped.spans) * 1000, RATE)
    else:
        end = start + Fraction(track["frames"] * 1000, track["sampleRate"])
    return start, end


def separate(tracks):
    """Keep input order; assign sorted spans to the lowest disjoint speaker lane.

    Lane IDs are the R64 proposal, pending shared review. Reserve every ordinary
    speaker ID first, since an opaque source ID can itself contain our suffix.
    Unknown offsets get their own lane; no timing or PCM is changed.
    """
    result = [dict(track) for track in tracks]
    grouped = {}
    reserved = {track["speaker"] for track in result}
    for track in result:
        if track.get("kind") not in ("microphone", "local", "system", "original"):
            grouped.setdefault(track["speaker"], []).append(track)
    diagnostics = []
    for speaker in sorted(grouped):
        rows = grouped[speaker]
        if len(rows) < 2:
            continue
        extents = {track["id"]: bounds(track) for track in rows}
        rows.sort(key=lambda t: (extents[t["id"]] is None, extents[t["id"]] or (), t["id"]))
        ends, ids = [], []
        suffix = 1
        for track in rows:
            interval = extents[track["id"]]
            lane = next(
                (i for i, end in enumerate(ends) if interval is not None and end is not None and end <= interval[0]),
                len(ends),
            )
            if lane == len(ends):
                if not ids:
                    identifier = speaker
                else:
                    identifier = f"{speaker}:overlap:{suffix}"
                    while identifier in reserved:
                        suffix += 1
                        identifier = f"{speaker}:overlap:{suffix}"
                    if len(identifier) > 256:
                        raise ValueError("Conflicting source speaker identifier exceeds metadata limit")
                    reserved.add(identifier)
                    suffix += 1
                ids.append(identifier)
                ends.append(None)
            ends[lane] = interval[1] if interval is not None else None
            track["speaker"] = ids[lane]
        if len(ids) > 1:
            diagnostics.append(
                dict(
                    code="source-spans-separated",
                    speaker=speaker,
                    tracks=[track["id"] for track in rows],
                    speakers=ids,
                    unknownOffset=any(interval is None for interval in extents.values()),
                )
            )
    return result, diagnostics
