#!/usr/bin/env python3
"""Check snapshot integrity, exact vector reports and D24 native-golden reports."""

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(path):
    def invalid(value):
        raise ValueError("Non-standard JSON constant: " + value)

    return json.loads(path.read_text(), parse_constant=invalid)


def integrity():
    for line in (HERE / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split()
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == expected, name
    vectors = load(HERE / "assign-vectors.json")
    cases = vectors["cases"]
    assert len(cases) == 22 and len({case["id"] for case in cases}) == len(cases)
    golden, pins = load(HERE / "golden.json"), load(HERE / "fetch-pins.json")
    assert golden["input"]["frames"] == pins["frames"] == 651840
    assert golden["input"]["sha256"] == pins["wavSHA256"]
    assert golden["input"]["pcmSHA256"] == pins["pcmSHA256"]
    assert golden["expected"]["wordCount"] == len(golden["expected"]["words"]) == 113
    assert (
        golden["expected"]["speakerCount"] == len(golden["expected"]["voiceIds"]) == 2
    )
    assert golden["tolerance"] == {
        "sameNumberOfVoices": True,
        "minimumWordVoiceAgreement": 0.98,
        "maxTurnBoundaryDeltaMs": 160,
    }
    return vectors, golden


def assess(actual, golden):
    expected = golden["expected"]
    assert actual["inputSHA256"] == golden["input"]["sha256"], "Input WAV hash differs"
    assert actual["modelSourceSHA256"] == golden["model"]["source_sha256"], (
        "Model revision differs"
    )
    assert actual["speakerCount"] == expected["speakerCount"], (
        "Number of voices differs"
    )
    words = actual["wordSpeakerIds"]
    assert len(words) == len(expected["words"]), (
        "Number of frozen word assignments differs"
    )
    assert all(word in expected["voiceIds"] for word in words), "Unknown voice ID"
    mismatches = [
        {
            "index": i,
            "text": word["text"],
            "expected": word["speakerId"],
            "actual": found,
        }
        for i, (word, found) in enumerate(zip(expected["words"], words, strict=True))
        if word["speakerId"] != found
    ]
    expected_turns = sorted(
        expected["turns"], key=lambda t: (t["speakerId"], t["startMs"], t["endMs"])
    )
    actual_turns = sorted(
        actual["turns"], key=lambda t: (t["speakerId"], t["startMs"], t["endMs"])
    )
    assert len(actual_turns) == len(expected_turns), (
        "Turn count differs; report instead of tuning"
    )
    deltas = []
    for turn, want in zip(actual_turns, expected_turns, strict=True):
        assert turn["speakerId"] == want["speakerId"], (
            "Corresponding turn voice differs"
        )
        assert all(type(turn[key]) is int for key in ("startMs", "endMs")), (
            "Normalize to integer D24 milliseconds first"
        )
        deltas.extend(abs(turn[key] - want[key]) for key in ("startMs", "endMs"))
    result = {
        "wordCount": len(words),
        "wordMatches": len(words) - len(mismatches),
        "wordMismatches": mismatches,
        "wordAgreement": (len(words) - len(mismatches)) / len(words),
        "maxTurnBoundaryDeltaMs": max(deltas, default=0),
    }
    assert (len(words) - len(mismatches)) * 100 >= len(words) * 98, json.dumps(result)
    assert result["maxTurnBoundaryDeltaMs"] <= 160, json.dumps(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actual", type=Path, help="Native golden report described in README.md"
    )
    parser.add_argument(
        "--vectors",
        type=Path,
        help="Map from case id to the exact independently computed expected-shaped output",
    )
    args = parser.parse_args()
    vectors, golden = integrity()
    if args.vectors:
        actual = load(args.vectors)
        for case in vectors["cases"]:
            assert actual[case["id"]] == case["expected"], case["id"]
    if args.actual:
        print(json.dumps(assess(load(args.actual), golden), sort_keys=True))
    else:
        print(
            "Verified22 cases,113 frozen word assignments,2-voice golden and snapshot hashes"
        )
