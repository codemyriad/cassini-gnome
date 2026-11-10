#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Summarize stored completed native-input runs; no runtime timing budgets."""

import argparse
import json
import statistics
from pathlib import Path


def distribution(values):
    values = sorted(values)
    return (
        dict(
            samples=len(values),
            median=statistics.median(values),
            p90=values[min(len(values) - 1, int(len(values) * 0.9))],
            maximum=max(values),
        )
        if values
        else None
    )


def summarize(case):
    raw = json.loads((case / "final.json").read_text())
    result = {
        key: raw[key] for key in ("firstRowsMs", "builtAndPaintedMs", "rssKiB", "maxRssKiB", "nativeKinetic", "model")
    }
    result["container"] = raw.get("container", case.name)
    result["sourceHashes"] = raw.get("sourceHashes", {})
    result["loadPaintCpuMs"] = distribution([x["paintCpuMs"] for x in raw["frames"] if x["phase"] == "opening"])
    result["loadIntervalCpuMs"] = distribution([x["intervalCpuMs"] for x in raw["frames"] if x["phase"] == "opening"])
    result["scroll"] = {}
    for phase in ("paused", "playback"):
        sent = json.loads((case / (phase + "-input.json")).read_text())
        ended = json.loads((case / (phase + "-coast-complete.json")).read_text())["us"]
        frames = [x for x in raw["frames"] if x["phase"] == phase]
        begin = sent[0][0]
        groups = []
        for stroke in range(6):
            start = min(x[0] for x in sent if x[1] == stroke)
            finish = min(x[0] for x in sent if x[1] == stroke + 1) if stroke < 5 else ended
            direction = 1 if stroke < 4 else -1
            received = [x for x in raw["input"] if start <= x["us"] < finish and direction * x["dy"] > 0]
            groups.append(len(received))
        assert all(groups), (case, phase, "a sent flick was not received", groups)
        assert any(x["direction"] > 0 for x in frames) and any(x["direction"] < 0 for x in frames)
        assert max(x["value"] for x in frames) > min(x["value"] for x in frames)
        before = [x for x in raw["frames"] if x["us"] < begin and x["anchor"] is not None]
        previous = before[-1] if before else None
        reversals = []
        for frame in frames:
            if frame["us"] < begin:
                continue
            # Direction changes are intentional new gestures; compare to a
            # baseline at rest or to the same received direction.
            if (
                previous
                and frame["anchor"] is not None
                and previous["anchor"] is not None
                and frame["direction"]
                and (previous["us"] < begin or previous["direction"] == frame["direction"])
            ):
                a, b = previous["anchor"], frame["anchor"]
                d = frame["direction"]
                if d * (b[0] - a[0]) < 0 or (a[0] == b[0] and d * (b[1] - a[1]) < -1):
                    reversals.append(dict(us=frame["us"], before=a, after=b, direction=d))
            previous = frame
        uppers = {x["upper"] for x in frames}
        result["scroll"][phase] = dict(
            frames=len(frames),
            receivedPerFlick=groups,
            reverseFrames=len(reversals),
            reversals=reversals,
            upperVariants=len(uppers),
            upper=min(uppers),
            paintCpuMs=distribution([x["paintCpuMs"] for x in frames]),
            intervalCpuMs=distribution([x["intervalCpuMs"] for x in frames]),
        )
    result["highlightCpuMs"] = distribution([x["cpuMs"] for x in raw["highlightCpuMs"] if x["phase"] == "playback"])
    end_narrow = json.loads((case / "resize-narrow.json").read_text())["us"]
    result["resize"] = {}
    for label, file in (("narrow", "resize-narrow-result.json"), ("wide", "resize-wide-result.json")):
        report = json.loads((case / file).read_text())
        frames = [x for x in report["frames"] if x["phase"] == "resize" and (label == "narrow" or x["us"] > end_narrow)]
        result["resize"][label] = dict(
            paintCpuMs=distribution([x["paintCpuMs"] for x in frames]),
            intervalCpuMs=distribution([x["intervalCpuMs"] for x in frames]),
            finalAnchor=frames[-1]["anchor"],
            upper=frames[-1]["upper"],
        )
    result["features"] = json.loads((case / "features.json").read_text())
    result["followAnchor"] = json.loads((case / "follow-position.json").read_text())["state"][2]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shared", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = ("final-lb60", "final-lb240", "final-lbplain", "final-box60", "final-box240", "final-boxplain")
    result = {name: summarize(args.shared / name) for name in cases}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for name, r in result.items():
        print(
            name,
            "open",
            round(r["firstRowsMs"], 1),
            round(r["builtAndPaintedMs"], 1),
            "reverse",
            r["scroll"]["paused"]["reverseFrames"],
            r["scroll"]["playback"]["reverseFrames"],
            "RSSMiB",
            round(r["rssKiB"] / 1024, 1),
        )
