#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the S-9 snapshot and independently produced implementation reports."""

import argparse
import array
import hashlib
import json
import math
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(path):
    return json.loads(Path(path).read_text())


def compare(actual, expected, tolerance, path="root"):
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        assert type(actual) is type(expected) and actual == expected, (path, actual, expected)
    elif isinstance(expected, (int, float)):
        assert type(actual) in (int, float) and math.isfinite(actual) and abs(actual - expected) <= tolerance, (
            path,
            actual,
            expected,
        )
    elif isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), (path, actual, expected)
        for key, value in expected.items():
            compare(actual[key], value, tolerance, f"{path}.{key}")
    else:
        assert isinstance(actual, list) and len(actual) == len(expected), (path, actual, expected)
        for index, e in enumerate(expected):
            compare(actual[index], e, tolerance, f"{path}[{index}]")


def snapshot():
    for line in (ROOT / "SHA256SUMS").read_text().splitlines():
        wanted, name = line.split("  ", 1)
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == wanted, name
    vectors, golden = load(ROOT / "vectors.json"), load(ROOT / "golden.json")
    assert len(vectors["cases"]) == len({c["id"] for c in vectors["cases"]}) == 38
    assert sum(len(c["checks"]) for c in vectors["cases"]) == 65
    desktop = load(ROOT / "desktop-vectors.json")
    assert len(desktop["cases"]) == len({c["id"] for c in desktop["cases"]}) == 2
    s11 = ROOT / golden["input"]["s11Golden"]
    assert hashlib.sha256(s11.read_bytes()).hexdigest() == golden["input"]["s11GoldenSHA256"]
    assert golden["runtime"]["dimension"] == 512
    assert len(golden["prints"]) == 6 and len(golden["comparisons"]) == 15
    for row in golden["prints"]:
        assert sum(b - a for a, b in row["windows"]) == row["sampleCount"]
        assert row["seconds"] == row["sampleCount"] / 16000
        assert 4 <= row["seconds"] <= 45
        assert all(0 <= a < b <= golden["input"]["frames"] and 24000 <= b - a <= 160000 for a, b in row["windows"])
    return vectors, golden


def audio(path, golden):
    path = Path(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == golden["input"]["sha256"]
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 651840)
        pcm = wav.readframes(wav.getnframes())
    assert hashlib.sha256(pcm).hexdigest() == golden["input"]["pcmSHA256"]
    samples = array.array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    for row in golden["prints"]:
        values = array.array("f", (samples[i] / 32768 for a, b in row["windows"] for i in range(a, b)))
        if sys.byteorder != "little":
            values.byteswap()
        assert hashlib.sha256(values.tobytes()).hexdigest() == row["inputSHA256"], row["id"]
    print("PASS S-11 WAV/PCM and all six float32 segment-input hashes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=Path, help="Map of case id to its ordered actual check outputs")
    parser.add_argument("--desktop", type=Path, help="Independent outputs for the two confirmed desktop rules")
    parser.add_argument("--actual", type=Path, help="Independent native model report, as described in README")
    parser.add_argument("--audio", type=Path, help="Locally built S-11 two-speaker.wav; verify selected PCM hashes")
    args = parser.parse_args()
    vectors, golden = snapshot()
    print("PASS S-9 snapshot: 38 Android tests / 65 checks; six model clips / 15 comparisons")
    if args.vectors:
        wanted = {c["id"]: [v["expected"] for v in c["checks"]] for c in vectors["cases"]}
        compare(load(args.vectors), wanted, vectors["numberTolerance"])
        print("PASS all 38 logic vectors / 65 actual outputs")
    if args.desktop:
        desktop = load(ROOT / "desktop-vectors.json")
        compare(load(args.desktop), {c["id"]: c["expected"] for c in desktop["cases"]}, 0)
        print("PASS both desktop merge/meeting-key cases")
    if args.audio:
        audio(args.audio, golden)
    if args.actual:
        report = load(args.actual)
        assert report["inputSHA256"] == golden["input"]["sha256"]
        assert report["modelSourceSHA256"] == golden["model"]["source_sha256"]
        assert type(report["dimension"]) is int and report["dimension"] == 512
        actual = {p["id"]: p for p in report["prints"]}
        assert len(actual) == len(report["prints"]) == len(golden["prints"])
        for p in golden["prints"]:
            a = actual[p["id"]]
            compare(a["windows"], p["windows"], 0)
            assert a["inputSHA256"] == p["inputSHA256"]
            assert math.isfinite(a["norm"]) and abs(a["norm"] - 1) <= 1e-5
        actual = {(p["a"], p["b"]): p["cosine"] for p in report["comparisons"]}
        assert len(actual) == len(report["comparisons"]) == len(golden["comparisons"])
        for p in golden["comparisons"]:
            score = actual[(p["a"], p["b"])]
            assert math.isfinite(score) and abs(score - p["cosine"]) <= golden["tolerance"]["cosineAbsolute"], (
                p,
                score,
            )
        compare(report["matches"], {m["id"]: m["expected"] for m in golden["matches"]}, 0)
        print("PASS independent model inputs, unit vectors, 15 cosines ±0.02 and exact AUTO/SUGGESTED outcomes")


if __name__ == "__main__":
    main()
