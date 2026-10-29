# SPDX-License-Identifier: GPL-3.0-or-later
"""D16: strict input/segment/count/text, then one-frame timing and trailing punctuation."""

import hashlib
import json

GOLDENS = {
    "parakeet-golden.json": "a6fda769d0e729cca54dc05d50c9803290e1f66a4643afdfa04a66d12cf58a3d",
    "parakeet-pipeline-golden.json": "8ded0bcea24bf93e93c713510afa4f9bd2ccfd9865e0c68061314b72749bcf4c",
}


def punctuation_match(actual, expected):
    # Strip at most one permitted trailing mark. Internal punctuation, case,
    # letters, digits, Unicode punctuation and other changes remain strict.
    def base(value):
        return value[:-1] if value[-1:] in (",", ".", "?", "!") else value

    return actual == expected or base(actual) == base(expected)


def assess(progress, golden_bytes, name):
    if name not in GOLDENS or hashlib.sha256(golden_bytes).hexdigest() != GOLDENS[name]:
        raise ValueError("Parakeet golden SHA256 mismatch")
    golden = json.loads(golden_bytes)
    structural = ("startSample", "endSample", "reason")
    actual_segments = [tuple(e["segment"][key] for key in structural) for e in progress]
    expected_segments = [tuple(e[key] for key in structural) for e in golden["segments"]]
    hashes = [e["inputSha256"] for e in progress]
    inputs_match = actual_segments == expected_segments and hashes == [e["inputSha256"] for e in golden["segments"]]
    words = [w for event in progress for w in event["words"]]
    expected = golden["words"]
    # Timing tolerance is unavailable until ALL segment hashes and boundaries
    # match. Never use timing/text normalisation to hide different audio input.
    if not inputs_match or len(words) != len(expected):
        return dict(accepted=False, segmentInputsMatch=inputs_match, wordCountMatches=len(words) == len(expected))
    differences = [
        dict(index=i, actual=a, expected=b) for i, (a, b) in enumerate(zip(words, expected, strict=True)) if a != b
    ]
    text_ok = all(punctuation_match(a["text"], b["text"]) for a, b in zip(words, expected, strict=True))
    maximum = max(
        (abs(a[k] - b[k]) for a, b in zip(words, expected, strict=True) for k in ("startMs", "endMs")), default=0
    )
    # Engine segment text and token text must also agree, apart from whitespace.
    coherent = all(" ".join(e["segment"]["text"].split()) == " ".join(w["text"] for w in e["words"]) for e in progress)
    return dict(
        accepted=text_ok and coherent and maximum <= 80,
        segmentInputsMatch=True,
        wordCountMatches=True,
        textMatches=text_ok,
        segmentTextCoherent=coherent,
        maxBoundaryDeltaMs=maximum,
        differences=differences,
    )
