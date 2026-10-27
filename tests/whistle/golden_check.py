# SPDX-License-Identifier: GPL-3.0-or-later
"""D11.3: a single frame of timing variance needs identical engine-input hashes."""


def assess_words(actual, expected, actual_hashes, expected_hashes, tolerance_ms=80):
    count_matches = len(actual) == len(expected)
    texts_match = count_matches and all(a["text"] == b["text"] for a, b in zip(actual, expected, strict=True))
    max_delta = max(
        (abs(a[key] - b[key]) for a, b in zip(actual, expected, strict=False) for key in ("startMs", "endMs")),
        default=0,
    )
    hashes_match = bool(expected_hashes) and actual_hashes == expected_hashes
    timing_matches = count_matches and max_delta <= tolerance_ms
    return dict(
        wordCountMatches=count_matches,
        wordTextMatches=texts_match,
        exactWords=actual == expected,
        maxBoundaryDeltaMs=max_delta,
        segmentInputHashesMatch=hashes_match,
        timingWithin80ms=timing_matches,
        accepted=texts_match and timing_matches and hashes_match,
    )
