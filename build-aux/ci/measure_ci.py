#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure all latest PR-run attempts for a commit, retaining per-job evidence."""

import argparse
import json
import math
import re
import statistics
import subprocess
from datetime import datetime
from pathlib import Path


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize(jobs):
    finished = [j for j in jobs if j["executionSeconds"] is not None]
    waits = [j["queueSeconds"] for j in jobs if j["queueSeconds"] is not None]
    return dict(
        jobCount=len(jobs),
        completedJobs=len(finished),
        executionMinutes=sum(j["executionSeconds"] for j in finished) / 60,
        estimatedRoundedLinuxMinutes=sum(math.ceil(j["executionSeconds"] / 60) for j in finished),
        medianQueueSeconds=statistics.median(waits) if waits else None,
        note="Linux per-job rounded minute estimate, not an invoice. Queue is started_at minus job created_at.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--repo", default="codemyriad/cassini-gnome")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", args.commit):
        parser.error("An exact commit SHA is required")

    def api(path):
        return json.loads(subprocess.check_output(["gh", "api", "repos/" + args.repo + path]))

    runs = api("/actions/runs?event=pull_request&head_sha=" + args.commit + "&per_page=100")["workflow_runs"]
    report = dict(commit=args.commit, event="pull_request", runs=[], jobs=[])
    for run in runs:
        # A single workflow here has at most 15 jobs. Fail clearly rather than
        # silently truncating future larger matrices.
        data = api("/actions/runs/" + str(run["id"]) + "/jobs?filter=latest&per_page=100")
        if data["total_count"] > 100:
            raise ValueError("Job pagination is required for this larger matrix")
        report["runs"].append({k: run[k] for k in ("id", "name", "status", "conclusion", "run_attempt", "html_url")})
        for job in data["jobs"]:
            item = {
                k: job[k]
                for k in (
                    "id",
                    "name",
                    "run_id",
                    "status",
                    "conclusion",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "labels",
                )
            }
            item["queueSeconds"] = (
                (stamp(job["started_at"]) - stamp(job["created_at"])).total_seconds() if job["started_at"] else None
            )
            item["executionSeconds"] = (
                (stamp(job["completed_at"]) - stamp(job["started_at"])).total_seconds()
                if job["completed_at"] and job["started_at"]
                else None
            )
            report["jobs"].append(item)
    report["summary"] = summarize(report["jobs"])
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
