# SPDX-License-Identifier: GPL-3.0-or-later
"""G3: the Meson pytest shards run every test file exactly once."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import shard_plan

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def present_files():
    return sorted(
        path.relative_to(TESTS).as_posix() for path in TESTS.rglob("test_*.py") if "node_modules" not in path.parts
    )


def meson_shards():
    return int(re.search(r"^pytest_shards = (\d+)$", (ROOT / "src/meson.build").read_text(), re.M)[1])


def test_every_test_file_lands_in_exactly_one_shard():
    files = present_files()
    count = meson_shards()
    shards = shard_plan.assign(files, count)
    assert sorted(shards) == files
    assert set(shards.values()) == set(range(1, count + 1))


def test_the_durations_table_lists_only_existing_test_files():
    stale = sorted(set(shard_plan.durations()) - set(present_files()))
    assert not stale, f"remove from tests/shard-durations.json (or regenerate it): {stale}"


def test_a_new_file_never_moves_a_known_one_and_lands_deterministically():
    table = {"test_a.py": 30.0, "test_b.py": 20.0, "test_c.py": 10.0, "test_d.py": 5.0}
    before = shard_plan.assign(sorted(table), 3, table)
    after = shard_plan.assign([*sorted(table), "test_new.py", "test_newer.py"], 3, table)
    assert {name: after[name] for name in table} == before
    assert after == shard_plan.assign(["test_newer.py", *table, "test_new.py"], 3, table)
    # Longest first to the least loaded: a=1, b=2, c=3, d=3 (15 s); then the
    # newcomers, in name order, each counted at the table's median (15 s).
    assert before == {"test_a.py": 1, "test_b.py": 2, "test_c.py": 3, "test_d.py": 3}
    assert (after["test_new.py"], after["test_newer.py"]) == (3, 2)


def collecting_shards(path, assignment, count=4):
    """The shards whose collection keeps `path`, by tests/conftest.py's hook."""
    import conftest

    kept = []
    for index in range(1, count + 1):
        shard = SimpleNamespace(index=index, count=count, assignment=assignment)
        config = SimpleNamespace(stash={conftest._shard: shard}, getini=lambda _name: ["test_*.py", "*_test.py"])
        if conftest.pytest_ignore_collect(path, config) is None:
            kept.append(index)
    return kept


def test_a_test_file_the_plan_did_not_list_still_runs_in_exactly_one_shard(tmp_path):
    here = Path(__file__)
    assert collecting_shards(here, {"test_shard_plan.py": 3}) == [3]
    # pytest follows a symlinked directory the plan's walk does not: without
    # the fallback such a file ran in no shard (resolving inside tests/) or in
    # every shard (resolving outside it).
    assert len(collecting_shards(here, {})) == 1
    outside = tmp_path / "test_outside.py"
    outside.write_text("")
    assert len(collecting_shards(outside, {})) == 1
    helper = tmp_path / "helper.py"
    helper.write_text("")
    assert collecting_shards(helper, {}) == [1, 2, 3, 4]  # Not a test file: the hook leaves it to pytest.


@pytest.mark.parametrize("value", ["0/4", "5/4", "1", "a/b"])
def test_a_malformed_shard_is_refused(value):
    with pytest.raises(ValueError):
        shard_plan.parse(value)


def test_check_distros_requires_every_meson_shard():
    script = (ROOT / "build-aux/check-distros.sh").read_text()
    listed = re.search(r"^for t in (.*); do$", script, re.M)[1].split()
    assert [name for name in listed if name.startswith("pytest")] == [
        f"pytest-{i}" for i in range(1, meson_shards() + 1)
    ]


@pytest.mark.skipif(not os.environ.get("CASSINI_TEST_SHARD"), reason="only under Meson's pytest-N shards")
def test_a_shard_has_a_private_home_xdg_dirs_and_basetemp(tmp_path):
    prefix = f"cassini-pytest-{os.environ['CASSINI_TEST_SHARD'].replace('/', 'of')}-"
    roots = [parent for parent in tmp_path.parents if parent.name.startswith(prefix)]
    assert len(roots) == 1, tmp_path
    root = roots[0]
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[name]).is_relative_to(root), name
    assert Path(os.environ["XDG_RUNTIME_DIR"]).stat().st_mode & 0o777 == 0o700
