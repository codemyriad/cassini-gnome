# SPDX-License-Identifier: GPL-3.0-or-later
"""build-aux/ci/cache_hygiene.py: which Actions cache entries are deleted."""

import datetime as dt
import importlib.util
import itertools
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cache_hygiene", Path(__file__).parents[1] / "build-aux/ci/cache_hygiene.py"
)
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)
NOW = dt.datetime(2026, 11, 12, 20, tzinfo=dt.UTC).timestamp()


_ids = itertools.count(1)


def entry(key, hours_ago, ref="refs/heads/main"):
    used = dt.datetime.fromtimestamp(NOW - hours_ago * 3600, dt.UTC).isoformat().replace("+00:00", "Z")
    return {"id": next(_ids), "key": key, "ref": ref, "last_accessed_at": used, "size_in_bytes": 1}


def deleted(caches, open_prs=()):
    return sorted((c["ref"], c["key"]) for c, _ in hygiene.select(caches, set(open_prs), NOW))


def test_family_strips_hash_and_week():
    assert hygiene.family("harness-desktop-2026-W46-2318ad0367b33ea3") == "harness-desktop"
    assert hygiene.family("parakeet-ultra-v2-" + "a" * 64) == "parakeet-ultra-v2"
    assert hygiene.family("desktop-gnome50-v1") == "desktop-gnome50-v1"


def test_superseded_key_goes_only_when_its_family_has_a_current_key():
    old, new = "harness-whistle-2026-W46-" + "1" * 16, "harness-whistle-2026-W46-" + "2" * 16
    assert deleted([entry(old, 50), entry(new, 2)]) == [("refs/heads/main", old)]
    assert deleted([entry(old, 50), entry(new, 30)]) == []  # no current key: GitHub's 7 days decide


def test_every_entry_of_a_current_key_is_kept():
    # one key, two cache versions (GitHub-hosted and self-hosted): the older-looking one stays
    key = "browser-startup-linux-x64-" + "c" * 64
    assert deleted([entry(key, 2), entry(key, 40)]) == []


def test_closed_pull_request_caches_go_open_ones_stay():
    caches = [
        entry("nemotron-" + "d" * 64, 1, "refs/pull/303/merge"),
        entry("nemotron-" + "d" * 64, 1, "refs/pull/412/merge"),
    ]
    assert deleted(caches, open_prs={412}) == [("refs/pull/303/merge", "nemotron-" + "d" * 64)]
