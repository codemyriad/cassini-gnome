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


TEXT_EXCEPTIONS_SHA256 = "fde2d870983855c11833703772d3a5c0a0ad79f9b54752442b1d6131914f09a3"


def load_text_exceptions(path=None):
    import hashlib
    import json
    from pathlib import Path

    path = path or Path(__file__).with_name("whistle-text-exceptions.json")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TEXT_EXCEPTIONS_SHA256:
        raise ValueError("D11.5 exception SHA256 mismatch")
    return json.loads(raw)


def assess_pipeline(actual, segments, golden_bytes, name, actual_hashes, expected_hashes, engine):
    """D11.5: only the published word and whole segment text, under all pins."""
    import hashlib
    import json

    contract = load_text_exceptions()
    golden = json.loads(golden_bytes)
    words = [dict(word) for word in actual]
    normalized = [dict(segment) for segment in segments]
    pins = contract["enginePins"]
    applied = False
    if (
        engine
        == dict(version=pins["engineVersion"], model=pins["modelSha256"], library=pins["linuxX86_64LibneedleSha256"])
        and actual_hashes == expected_hashes
    ):
        for rule in contract["exceptions"]:
            i, word = rule["segmentIndex"], rule["wordIndex"]
            if not (
                name == rule["golden"]
                and hashlib.sha256(golden_bytes).hexdigest() == rule["goldenSha256"]
                and len(segments) == len(golden["segments"])
                and len(actual) == len(golden["words"])
                and len(actual_hashes) > i
                and actual_hashes[i] == rule["segmentInputSha256"]
                and segments[i]["startSample"] == rule["startSample"]
                and segments[i]["endSample"] == rule["endSample"]
                and golden["segments"][i]["text"] == rule["expectedSegmentText"]
                and golden["words"][word]["text"] == rule["expectedWord"]
                and words[word]["text"] in rule["allowedWords"]
                and segments[i]["text"]
                == (
                    rule["expectedSegmentText"]
                    if words[word]["text"] == rule["expectedWord"]
                    else rule["actualSegmentText"]
                )
            ):
                continue
            applied = (
                words[word]["text"] != rule["expectedWord"] or normalized[i]["text"] != rule["expectedSegmentText"]
            )
            words[word]["text"] = rule["expectedWord"]
            normalized[i]["text"] = rule["expectedSegmentText"]
    result = assess_words(words, golden["words"], actual_hashes, expected_hashes)
    result["textConforms"] = result["wordTextMatches"]
    result["wordTextMatches"] = [w["text"] for w in actual] == [w["text"] for w in golden["words"]]
    result["exactWords"] = actual == golden["words"]
    result["exactSegments"] = segments == golden["segments"]
    result["segmentsConform"] = normalized == golden["segments"]
    result["exceptionApplied"] = applied
    result["textExceptionPolicy"] = "D11.5"
    result["textExceptionSha256"] = TEXT_EXCEPTIONS_SHA256
    result["accepted"] = result["accepted"] and result["segmentsConform"]
    return result
