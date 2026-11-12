#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Delete Actions cache entries nothing will read again, so least-recently-used eviction (the repository's 10 GB
quota) never pushes out a hot entry (GNOME-CI-CACHING.md item 5).

Deleted:
  - every cache of a pull request that is closed or merged (refs/pull/N/merge);
  - on main, the superseded keys of a family: a family is a key without its trailing content hash and ISO-week
    epoch (harness-desktop-2026-W46-2318ad0367b33ea3 -> harness-desktop). A key is current if any entry of it
    was used in the last KEEP_HOURS; a family's other keys are deleted only when the family has a current key.
Never deleted: any entry of a current key. One key can have several entries (cache versions differ by runner,
e.g. GitHub-hosted and self-hosted), so entries are judged by key, never one by one; nor the newest key of a
family that has no current key (GitHub evicts those after 7 unused days anyway).

  build-aux/ci/cache_hygiene.py [--delete]     without --delete: only print what would be deleted
Needs GITHUB_REPOSITORY and GITHUB_TOKEN (actions: write to delete, pull-requests: read)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request

KEEP_HOURS = 24
API = "https://api.github.com"


def family(key: str) -> str:
    key = re.sub(r"-[0-9a-f]{16,}$", "", key)
    return re.sub(r"-\d{4}-W\d{2}$", "", key)


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def select(caches: list[dict], open_prs: set[int], now: float) -> list[tuple[dict, str]]:
    """(entry, reason) for each entry to delete."""
    out = []
    main = [c for c in caches if c["ref"] in ("refs/heads/main", "refs/heads/master")]
    for c in caches:
        m = re.fullmatch(r"refs/pull/(\d+)/merge", c["ref"])
        if m and int(m[1]) not in open_prs:
            out.append((c, f"pull request #{m[1]} is closed"))
    last_used: dict[str, float] = {}
    for c in main:
        last_used[c["key"]] = max(last_used.get(c["key"], 0.0), ts(c["last_accessed_at"]))
    by_family: dict[str, list[str]] = {}
    for key in last_used:
        by_family.setdefault(family(key), []).append(key)
    for fam, keys in by_family.items():
        current = {k for k in keys if now - last_used[k] < KEEP_HOURS * 3600}
        if not current:
            continue
        for c in main:
            if family(c["key"]) == fam and c["key"] not in current:
                out.append((c, f"superseded in family {fam} (unused {(now - last_used[c['key']]) / 3600:.0f} h)"))
    return out


def api(method: str, path: str):
    req = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--delete", action="store_true", help="delete (default: only print)")
    a = ap.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    caches, page = [], 1
    while True:
        batch = api("GET", f"/repos/{repo}/actions/caches?per_page=100&page={page}")["actions_caches"]
        caches += batch
        if len(batch) < 100:
            break
        page += 1
    open_prs, page = set(), 1
    while True:
        batch = api("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page={page}")
        open_prs |= {p["number"] for p in batch}
        if len(batch) < 100:
            break
        page += 1
    doomed = select(caches, open_prs, dt.datetime.now(dt.UTC).timestamp())
    total = sum(c["size_in_bytes"] for c in caches)
    freed = sum(c["size_in_bytes"] for c, _ in doomed)
    for c, why in doomed:
        print(f"{'delete' if a.delete else 'would delete'} {c['size_in_bytes'] / 1e6:6.0f} MB {c['ref']} {c['key']}: {why}")
        if a.delete:
            api("DELETE", f"/repos/{repo}/actions/caches/{c['id']}")
    print(f"{len(doomed)} of {len(caches)} entries, {freed / 1e9:.2f} of {total / 1e9:.2f} GB"
          f"{'' if a.delete else ' (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
