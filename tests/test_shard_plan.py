# SPDX-License-Identifier: GPL-3.0-or-later
"""G3: the Meson pytest shards run every test exactly once, by real pytest collection."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import shard_plan

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
NODE_ID = re.compile(r"^[\w./-]+\.py::")


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


def check_meson_tests():
    path = ROOT / "build-aux/ci/check_meson_tests.py"
    spec = importlib.util.spec_from_file_location("check_meson_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def introspected(shards=4, suite="cassini-talk:pytest", extra=()):
    """`meson introspect --tests` entries, as every Meson from 1.3 writes them."""
    tests = [{"name": name, "suite": ["cassini-talk"]} for name in check_meson_tests().REQUIRED]
    tests += [{"name": f"pytest-{index}", "suite": [suite]} for index in range(1, shards + 1)]
    return tests + [*extra]


def test_check_distros_reads_the_test_list_from_meson_introspection():
    script = (ROOT / "build-aux/check-distros.sh").read_text()
    assert "meson introspect /build/b --tests > /build/tests.json" in script
    assert "python3 build-aux/ci/check_meson_tests.py /build/tests.json" in script
    assert "test -C /build/b --list" not in script  # Its text differs between Meson releases.
    check = check_meson_tests()
    assert check.shard_count() == meson_shards()
    assert check.problems(introspected(meson_shards()), meson_shards()) == []


@pytest.mark.parametrize(
    "tests",
    [
        introspected(3),  # A shard missing.
        introspected(5),  # A shard src/meson.build does not declare.
        introspected(suite="cassini-talk"),  # The shards outside the pytest suite.
        introspected()[1:],  # A tool's test missing.
    ],
)
def test_check_meson_tests_refuses_an_incomplete_build(tests, tmp_path):
    check = check_meson_tests()
    assert check.problems(tests, 4)
    listing = tmp_path / "tests.json"
    listing.write_text(json.dumps(tests))
    script = [sys.executable, ROOT / "build-aux/ci/check_meson_tests.py", listing]
    assert subprocess.run(script, capture_output=True, timeout=60).returncode == 1


@pytest.mark.skipif(not os.environ.get("CASSINI_TEST_BUILD"), reason="needs Meson's build directory")
def test_this_build_registers_every_shard():
    # meson-info/intro-tests.json is what `meson introspect --tests` prints.
    tests = json.loads((Path(os.environ["CASSINI_TEST_BUILD"]) / "meson-info/intro-tests.json").read_text())
    shards = sorted(test["name"] for test in tests if "pytest" in check_meson_tests().suites(test))
    assert shards == sorted(f"pytest-{index}" for index in range(1, meson_shards() + 1))


def collect(cwd, shards):
    """Real `pytest --collect-only` in `cwd`: the whole suite (shard None) and
    each shard ("i/N"), concurrently; per run its exit code, node ids and output."""
    env = {name: value for name, value in os.environ.items() if not name.startswith(("CASSINI_TEST_", "PYTEST_"))}
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", "tests"]
    runs = [
        subprocess.Popen(
            command,
            cwd=cwd,
            env={**env, "CASSINI_TEST_SHARD": shard} if shard else env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for shard in shards
    ]
    results = []
    for run in runs:
        output = run.communicate(timeout=300)[0]
        ids = Counter(line for line in output.splitlines() if NODE_ID.match(line))
        results.append(SimpleNamespace(code=run.returncode, ids=ids, output=output))
    return results


def assert_shards_partition(full, shards):
    assert full.code == 0, full.output
    for index, run in enumerate(shards, 1):
        assert run.code == 0 and run.ids, f"shard {index}: {run.output}"
    union = sum((run.ids for run in shards), Counter())
    assert union - full.ids == Counter(), "collected by more shards than the suite"
    assert full.ids - union == Counter(), "collected by no shard"


def test_the_shards_collect_exactly_the_whole_suite_between_them():
    count = meson_shards()
    full, *shards = collect(ROOT, [None, *(f"{index}/{count}" for index in range(1, count + 1))])
    assert sum(full.ids.values()) > 1000, full.output
    assert_shards_partition(full, shards)


def sandbox(root, table, files):
    """A tests/ with the real conftest.py and shard_plan.py, its own durations
    table, and `files` ({relative path: source})."""
    tests = root / "tests"
    tests.mkdir(parents=True)
    for name in ("conftest.py", "shard_plan.py"):
        shutil.copy(TESTS / name, tests / name)
    (tests / "shard-durations.json").write_text(json.dumps({"files": table}))
    (root / "pytest.ini").write_text("[pytest]\n")  # The rootdir: no config above it applies.
    for name, source in files.items():
        (tests / name).parent.mkdir(parents=True, exist_ok=True)
        (tests / name).write_text(source)
    return root


PARAMETRIZED = """
import pytest

@pytest.mark.parametrize("value", [1, "two", None])
def test_value(value):
    pass

class TestGroup:
    def test_one(self):
        pass

    def test_two(self):
        pass
"""


def test_real_collection_runs_recorded_unrecorded_and_symlinked_files_exactly_once(tmp_path):
    table = {"test_a.py": 30.0, "test_b.py": 20.0, "test_c.py": 10.0, "sub/test_d.py": 5.0}
    root = sandbox(tmp_path / "copy", table, {name: PARAMETRIZED for name in [*table, "test_unrecorded.py"]})
    # Reached by pytest, not by the plan's walk: the conftest fallback's case.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_linked.py").write_text(PARAMETRIZED)
    (root / "tests/linked").symlink_to(outside, target_is_directory=True)
    full, *shards = collect(root, [None, "1/4", "2/4", "3/4", "4/4"])
    files = Counter(node.partition("::")[0] for node in full.ids)
    assert files == {f"tests/{name}": 5 for name in [*table, "test_unrecorded.py", "linked/test_linked.py"]}
    assert_shards_partition(full, shards)


def test_a_collection_error_fails_the_shard_that_owns_the_file(tmp_path):
    files = {"test_a.py": PARAMETRIZED, "test_b.py": PARAMETRIZED, "test_broken.py": "raise RuntimeError('broken')\n"}
    root = sandbox(tmp_path, {"test_a.py": 10.0, "test_b.py": 5.0}, files)
    full, *shards = collect(root, [None, "1/2", "2/2"])
    assert full.code != 0
    failed = [run for run in shards if run.code != 0]
    assert len(failed) == 1 and "test_broken.py" in failed[0].output, [run.output for run in shards]


def test_an_empty_shard_fails(tmp_path):
    root = sandbox(tmp_path, {"test_a.py": 10.0}, {"test_a.py": PARAMETRIZED})
    first, second = collect(root, ["1/2", "2/2"])
    assert first.code == 0 and first.ids
    assert second.code == pytest.ExitCode.NO_TESTS_COLLECTED and not second.ids  # 5: Meson counts it a failure.


@pytest.mark.skipif(not os.environ.get("CASSINI_TEST_SHARD"), reason="only under Meson's pytest-N shards")
def test_a_shard_has_a_private_home_xdg_dirs_and_basetemp(tmp_path):
    prefix = f"cassini-pytest-{os.environ['CASSINI_TEST_SHARD'].replace('/', 'of')}-"
    roots = [parent for parent in tmp_path.parents if parent.name.startswith(prefix)]
    assert len(roots) == 1, tmp_path
    root = roots[0]
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[name]).is_relative_to(root), name
    assert Path(os.environ["XDG_RUNTIME_DIR"]).stat().st_mode & 0o777 == 0o700
