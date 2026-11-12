# SPDX-License-Identifier: GPL-3.0-or-later
"""PR coverage/fail-open history, and exact build-product identity across jobs."""

import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_ci_release import module


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("docs/notes.md", set()),
        ("src/cassini_talk/player.py", {"boards", "library", "offline"}),
        ("src/cassini_talk/library.py", {"boards", "library", "offline"}),
        ("src/cassini_talk/parakeet.py", {"boards", "library", "offline"}),
        ("tests/parakeet/queue_probe.py", {"offline"}),
        ("tests/diarization/queue_probe.py", {"offline"}),
        ("tests/test_diarization_models.py", {"boards", "offline"}),
        ("tests/ux/diarization/golden.json", {"boards", "library", "offline"}),
        ("tests/ui/quit.py", {"boards"}),
        ("tests/ui/future-board.py", {"boards"}),
        ("tests/test_model_download.py", {"boards"}),
        ("tests/desktop_services.py", {"boards"}),
        ("tests/recording/readiness.py", {"boards"}),
        ("tests/conftest.py", {"boards"}),
        # G: tests Boards never reads no longer run it; the guard below keeps
        # the list complete as Boards' scripts change.
        ("tests/test_future_ui_dependency.py", set()),
        ("tests/new_helpers.py", set()),
        ("tests/recording/run.sh", set()),
        ("tests/ux/states-vectors.json", {"boards", "library"}),
        ("whistle/pins.json", {"boards", "offline"}),
        ("browser-extension/manifest.json", {"boards"}),
        ("build-aux/bounded-run.sh", {"boards", "library", "offline"}),
        ("data/io.codemyriad.CassiniTalk.gschema.xml", {"boards", "library", "offline"}),
        ("meson_options.txt", {"boards", "library", "offline"}),
        (".github/workflows/ci.yml", {"boards", "library", "offline"}),
        ("build-aux/ci/product.py", {"boards", "library", "offline"}),
    ],
)
def test_preserved_pr_path_coverage(path, extra):
    changes = module("changes")
    assert {area for area, enabled in changes.select([path]).items() if enabled} == {
        "source",
        "packages",
        "recording",
    } | extra


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_events_never_apply_path_filters(event):
    areas, _reason, _paths = module("changes").detect(event, {}, git=lambda _: pytest.fail("Non-PR queried git"))
    assert all(areas.values()) and len(areas) == 6


@pytest.mark.parametrize("payload", [{}, {"pull_request": {"base": {"sha": None}, "head": {"sha": "x"}}}])
def test_missing_or_unknown_base_runs_everything(payload):
    areas, reason, _paths = module("changes").detect("pull_request", payload)
    assert all(areas.values()) and "running everything" in reason


def test_failed_diff_runs_everything():
    def fail(_command):
        raise subprocess.CalledProcessError(128, "git diff")

    payload = {"pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}
    areas, _reason, _paths = module("changes").detect("pull_request", payload, git=fail)
    assert all(areas.values())


@pytest.mark.skipif(
    shutil.which("git") is None, reason="Git integration runs in the source/build jobs; this environment has no git"
)
def test_actual_git_rename_keeps_old_area_coverage(tmp_path):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git_in(repo, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=CI fixture", "-c", "user.email=ci@example.invalid", *args], cwd=repo, env=env
        )

    area_repo = tmp_path / "area"
    area_repo.mkdir()
    git_in(area_repo, "init", "--initial-branch=main")
    source = area_repo / "src/cassini_talk/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("fixture\n")
    git_in(area_repo, "add", ".")
    git_in(area_repo, "commit", "-m", "Before")
    base = git_in(area_repo, "rev-parse", "HEAD").decode().strip()
    source.rename(area_repo / "notes.txt")
    git_in(area_repo, "add", "-A")
    git_in(area_repo, "commit", "-m", "Moved outside area")
    head = git_in(area_repo, "rev-parse", "HEAD").decode().strip()
    changes = module("changes")
    payload = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    areas, _reason, paths = changes.detect(
        "pull_request", payload, git=lambda command: subprocess.check_output(command, cwd=area_repo, env=env)
    )
    assert areas["library"] and areas["offline"] and areas["boards"]
    assert set(paths) == {"src/cassini_talk/library.py", "notes.txt"}

    # Moving a packaging recipe out of its directory also enables its leg on unlabeled PRs.
    pkg_repo = tmp_path / "pkg"
    pkg_repo.mkdir()
    git_in(pkg_repo, "init", "--initial-branch=main")
    recipe = pkg_repo / "build-aux/flatpak/smoke.sh"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("#!/bin/sh\n")
    git_in(pkg_repo, "add", ".")
    git_in(pkg_repo, "commit", "-m", "Before")
    pkg_base = git_in(pkg_repo, "rev-parse", "HEAD").decode().strip()
    recipe.rename(pkg_repo / "notes.txt")
    git_in(pkg_repo, "add", "-A")
    git_in(pkg_repo, "commit", "-m", "Moved recipe")
    pkg_head = git_in(pkg_repo, "rev-parse", "HEAD").decode().strip()
    event_file, output_file, report_file = tmp_path / "pkg-event.json", tmp_path / "pkg-out", tmp_path / "pkg.json"
    event_file.write_text(
        json.dumps({"pull_request": {"labels": [], "base": {"sha": pkg_base}, "head": {"sha": pkg_head}}})
    )
    clean_env = {k: v for k, v in env.items() if not k.startswith("GITHUB_")}
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "build-aux/ci/changes.py"),
            "--event",
            "pull_request",
            "--payload",
            str(event_file),
            "--output",
            str(output_file),
            "--report",
            str(report_file),
        ],
        cwd=pkg_repo,
        check=True,
        env=clean_env,
    )
    parsed = json.loads(report_file.read_text())
    assert parsed["package_legs"] == {
        "deb_testing": False,
        "rpm_opensuse": False,
        "arch": False,
        "flatpak": True,
        "extra_distros": False,
    }

    # Real git merge --no-ff: identical tree skips verified PR desktop areas; skewed merge skips nothing.
    merge_repo = tmp_path / "merge"
    merge_repo.mkdir()
    git_in(merge_repo, "init", "--initial-branch=main")
    (merge_repo / "a.txt").write_text("base\n")
    git_in(merge_repo, "add", ".")
    git_in(merge_repo, "commit", "-m", "Base")
    git_in(merge_repo, "checkout", "-b", "pr-same")
    (merge_repo / "a.txt").write_text("pr\n")
    git_in(merge_repo, "add", ".")
    git_in(merge_repo, "commit", "-m", "PR same")
    pr_same = git_in(merge_repo, "rev-parse", "HEAD").decode().strip()
    git_in(merge_repo, "checkout", "main")
    git_in(merge_repo, "merge", "--no-ff", "-m", "Merge PR same", "pr-same")
    merge_same = git_in(merge_repo, "rev-parse", "HEAD").decode().strip()
    same_tree_real = git_in(merge_repo, "rev-parse", f"{merge_same}^{{tree}}").decode().strip()
    areas_same, desktop_same, _ = changes.apply_push_pr_tree_skips(
        "push",
        dict.fromkeys(changes.AREAS, True),
        repo="codemyriad/cassini-gnome",
        commit=merge_same,
        git=lambda cmd: subprocess.check_output(cmd, cwd=merge_repo, env=env),
        verifier=lambda _repo, sha, expected_tree: (
            frozenset({"recording", "desktop"}) if sha == pr_same and expected_tree == same_tree_real else frozenset()
        ),
    )
    assert areas_same["recording"] is False and areas_same["boards"] is True and desktop_same is False

    git_in(merge_repo, "checkout", "-b", "pr-skew", "HEAD~1")
    (merge_repo / "b.txt").write_text("skew\n")
    git_in(merge_repo, "add", ".")
    git_in(merge_repo, "commit", "-m", "PR skew")
    git_in(merge_repo, "checkout", "main")
    git_in(merge_repo, "merge", "--no-ff", "-m", "Merge PR skew", "pr-skew")
    merge_skew = git_in(merge_repo, "rev-parse", "HEAD").decode().strip()
    areas_skew, desktop_skew, _ = changes.apply_push_pr_tree_skips(
        "push",
        dict.fromkeys(changes.AREAS, True),
        repo="codemyriad/cassini-gnome",
        commit=merge_skew,
        git=lambda cmd: subprocess.check_output(cmd, cwd=merge_repo, env=env),
        verifier=lambda _repo, _sha, _tree: pytest.fail("Skewed merge must not query PR verifier"),
    )
    assert all(areas_skew.values()) and desktop_skew is True


def test_invalid_event_file_still_emits_all_outputs_and_success(tmp_path):
    payload = tmp_path / "event.json"
    payload.write_text("not JSON")
    output, report = tmp_path / "outputs", tmp_path / "report.json"
    script = Path(__file__).parents[1] / "build-aux/ci/changes.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--event",
            "pull_request",
            "--payload",
            str(payload),
            "--output",
            str(output),
            "--report",
            str(report),
        ],
        check=True,
        env=env,
    )
    parsed = json.loads(report.read_text())
    assert (
        all(parsed["areas"].values())
        and parsed["desktop"] is True
        and all(parsed["package_legs"].values())
        and parsed["full_ci"] is True
    )
    assert len(output.read_text().splitlines()) == 13 and "false" not in output.read_text()


def test_execution_tier_gates_pr_and_push_on_two_distro_legs_and_expands_on_full_ci_or_nightly():
    changes = module("changes")
    base_pr = {"pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}
    assert changes.execution_tier("pull_request", base_pr) is False
    assert (
        changes.execution_tier(
            "pull_request",
            {"pull_request": {**base_pr["pull_request"], "labels": [{"name": "not-full-ci"}, {"name": "ui"}]}},
        )
        is False
    )
    for label in ("full-ci", "FULL-CI"):
        labeled = {"pull_request": {**base_pr["pull_request"], "labels": [{"name": label}]}}
        assert changes.execution_tier("pull_request", labeled) is True
        areas, reason, paths = changes.detect("pull_request", labeled, git=lambda _: pytest.fail("full-ci queried git"))
        assert all(areas.values()) and "full-ci" in reason
        assert all(changes.resolve_package_legs("pull_request", full_ci=True, reason=reason, paths=paths).values())
    assert changes.execution_tier("push", {}) is False
    assert not any(
        changes.resolve_package_legs("push", full_ci=False, reason="Full run outside a PR", paths=[]).values()
    )
    for full_event in ("schedule", "workflow_dispatch"):
        assert changes.execution_tier(full_event, {}) is True
        assert all(
            changes.resolve_package_legs(full_event, full_ci=True, reason="Full run outside a PR", paths=[]).values()
        )
    # Malformed label payloads fail open to full CI and every package leg.
    for bad in ({}, {"pull_request": {"labels": "bad"}}, {"pull_request": {"labels": [{}]}}):
        assert changes.execution_tier("pull_request", bad) is True
        areas, reason, paths = changes.detect("pull_request", bad)
        assert all(areas.values())
        assert all(changes.resolve_package_legs("pull_request", full_ci=False, reason=reason, paths=paths).values())


ALL_PACKAGE_LEGS = {"deb_testing", "rpm_opensuse", "arch", "flatpak", "extra_distros"}


@pytest.mark.parametrize(
    ("changed_path", "expected_legs"),
    [
        # Per-recipe contract checks on unlabeled PRs (Codex #387):
        ("build-aux/flatpak/io.codemyriad.CassiniTalk.json", {"flatpak"}),
        ("build-aux/flatpak/io.codemyriad.CassiniTalk.Devel.json", {"flatpak"}),
        ("build-aux/flatpak/smoke.sh", {"flatpak"}),
        ("tests/flatpak/trash.py", {"flatpak"}),
        ("tests/desktop/run.sh", {"flatpak"}),
        ("build-aux/arch/PKGBUILD", {"arch"}),
        ("build-aux/arch/build.sh", {"arch"}),
        ("debian/rules", {"deb_testing"}),
        ("debian/control", {"deb_testing"}),
        ("build-aux/deb/build.sh", {"deb_testing"}),
        ("build-aux/deb/Dockerfile", {"deb_testing"}),
        ("build-aux/rpm/cassini-talk.spec", {"rpm_opensuse"}),
        ("build-aux/rpm/build.sh", {"rpm_opensuse"}),
        ("build-aux/check-distros.sh", {"extra_distros"}),
        ("build-aux/install-deps.sh", {"extra_distros"}),
        # Shared packaging inputs enable every affected package recipe:
        ("whistle/pins.json", {"deb_testing", "rpm_opensuse", "arch", "flatpak"}),
        ("src/cassini_talk/sherpa-pins.json", {"deb_testing", "rpm_opensuse", "arch", "flatpak"}),
        ("build-aux/smoke-installed.py", {"deb_testing", "rpm_opensuse", "arch", "flatpak"}),
        ("meson.build", ALL_PACKAGE_LEGS),
        (".github/workflows/flatpak.yml", ALL_PACKAGE_LEGS),
        ("build-aux/ci/prepare_tumbleweed.sh", ALL_PACKAGE_LEGS),
        # Non-packaging PRs stay on the fast two-distro default:
        ("src/cassini_talk/player.py", set()),
        ("tests/test_transcript.py", set()),
        ("docs/ci.md", set()),
        ("po/it.po", set()),
        ("data/io.codemyriad.CassiniTalk.gschema.xml", set()),
    ],
)
def test_unlabeled_packaging_pr_enables_affected_package_legs_per_recipe(tmp_path, changed_path, expected_legs):
    # Package builds run without git installed (check_pytest_ran.py SKIPS); use a
    # PATH git diff stub here so every package build runs all 26 contract cases.
    base, head = "a" * 40, "b" * 40
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/bin/sh\n"
        f'test "$*" = "diff --name-only --no-renames -z {base}...{head} --" || exit 2\n'
        'printf "%s\\0" "$CASSINI_TEST_CHANGED_PATH"\n'
    )
    git_stub.chmod(0o755)
    payload_file = tmp_path / "event.json"
    payload_file.write_text(json.dumps({"pull_request": {"labels": [], "base": {"sha": base}, "head": {"sha": head}}}))
    output_file, report_file = tmp_path / "outputs", tmp_path / "report.json"
    script = Path(__file__).parents[1] / "build-aux/ci/changes.py"
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    clean_env["PATH"] = f"{bin_dir}:{clean_env.get('PATH', '')}"
    clean_env["CASSINI_TEST_CHANGED_PATH"] = changed_path
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--event",
            "pull_request",
            "--payload",
            str(payload_file),
            "--output",
            str(output_file),
            "--report",
            str(report_file),
        ],
        cwd=tmp_path,
        check=True,
        env=clean_env,
    )
    parsed = json.loads(report_file.read_text())
    assert parsed["full_ci"] is False
    assert parsed["areas"]["packages"] is True
    assert set(parsed["package_legs"]) == ALL_PACKAGE_LEGS
    assert {leg for leg, enabled in parsed["package_legs"].items() if enabled} == expected_legs
    emitted = dict(line.split("=", 1) for line in output_file.read_text().splitlines())
    for leg in ALL_PACKAGE_LEGS:
        assert emitted[leg] == ("true" if leg in expected_legs else "false")
    assert emitted["full_ci"] == "false"


def test_every_package_leg_pattern_names_an_existing_file():
    root = Path(__file__).parents[1]
    package_legs = json.loads((root / "build-aux/ci/pr-paths.json").read_text())["package_legs"]
    assert set(package_legs) == ALL_PACKAGE_LEGS
    tracked = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
    missing = {
        leg: [pattern for pattern in patterns if not any(fnmatch.fnmatchcase(path, pattern) for path in tracked)]
        for leg, patterns in package_legs.items()
    }
    assert {leg: bad for leg, bad in missing.items() if bad} == {}


def test_product_reuse_preserves_executable_and_checks_commit_profile_and_digest(tmp_path):
    product = module("product")
    source = tmp_path / "source"
    source.mkdir()
    launcher = source / "launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    archive = tmp_path / "product.tar"
    product.pack(source, archive, "a" * 40, "boards")
    for commit, profile in (("b" * 40, "boards"), ("a" * 40, "core")):
        destination = tmp_path / "wrong"
        with pytest.raises(ValueError, match="commit/profile"):
            product.unpack(archive, destination, commit, profile)
        assert not destination.exists()
    output = tmp_path / "restored"
    product.unpack(archive, output, "a" * 40, "boards")
    assert (output / "launcher").stat().st_mode & 0o111
    assert (output / "launcher").read_bytes() == launcher.read_bytes()
    # Keep a valid tar and manifest, but alter a payload without its digest.
    with tarfile.open(archive, "a") as tar:
        data = b"changed"
        info = tarfile.TarInfo("product/launcher")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="digest"):
        product.unpack(archive, tmp_path / "corrupt", "a" * 40, "boards")
    assert not (tmp_path / "corrupt").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "product/../../escape"])
def test_product_rejects_traversal_before_extraction(tmp_path, name):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(name)
        tar.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="Unsafe"):
        module("product").unpack(archive, tmp_path / "restored", "a" * 40, "core")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "path",
    [
        "src/cassini_talk/model_download.py",
        "src/cassini_talk/model_manifest.py",
        "src/cassini_talk/zstd_stream.py",
        "src/cassini_talk/strings.json",
        "src/cassini_talk/portable_context.py",
        "src/cassini_talk/meeting_store.py",
        "src/cassini_talk/recording_session.py",
        "src/cassini_talk/future_dependency.py",
        "src/cassini-talk.gresource.xml",
        "build-aux/whistle-cache.sh",
    ],
)
def test_transitive_dependencies_run_both_engine_and_library_checks(path):
    selected = module("changes").select([path])
    assert selected["library"] and selected["offline"]


def test_boards_pr_filter_matches_every_file_boards_reads():
    # G (D20): Boards' PR filter lists the tests it reads, not tests/**. A
    # file Boards reads that the filter misses would let a PR changing only
    # that file skip Boards; build-aux/ci/boards_inputs.py --why PATH says
    # how the scan reached a file, --patterns tests/ prints the globs.
    boards_inputs = module("boards_inputs")
    files = boards_inputs.inputs()
    # The scan follows the workflow, its scripts, Python imports through
    # sys.path entries, path constants, pytest's conftest and the product.
    assert {
        "build-aux/ux-boards.sh",
        "build-aux/bounded-run.sh",
        "tests/ui/library.py",
        "tests/ui/recording-fixture.py",
        "tests/test_ux_vectors.py",
        "tests/conftest.py",
        "tests/desktop_services.py",
        "tests/recording/readiness.py",
        "tests/spectro-vectors/spectro-v1.golden.json",
        "ux/library-vectors/make.py",
        "src/cassini_talk/main.py",
        "whistle/pins.json",
    } <= set(files)
    policy = json.loads((Path(__file__).parents[1] / "build-aux/ci/pr-paths.json").read_text())
    assert boards_inputs.missing(policy["paths"]["boards"], files) == []


def test_every_boards_pattern_names_an_existing_file():
    # A stale or misspelled entry would hide a real gap behind a passing list.
    root = Path(__file__).parents[1]
    boards = json.loads((root / "build-aux/ci/pr-paths.json").read_text())["paths"]["boards"]
    tracked = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
    assert [pattern for pattern in boards if not any(fnmatch.fnmatchcase(path, pattern) for path in tracked)] == []


def test_boards_scan_follows_imports_constants_and_scripts(tmp_path):
    boards_inputs = module("boards_inputs")

    def write(path, text=""):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)

    write(".github/workflows/ux-shot.yml", "run: |\n  # tests/ignored.py is a comment\n  build-aux/boards.sh out\n")
    write("build-aux/boards.sh", 'python3 tests/ui/board.py\n"$(dirname "$0")"/bounded.sh\n')
    write("build-aux/bounded.sh")
    write(
        "tests/ui/board.py",
        "import sys\nfrom pathlib import Path\nROOT = Path(__file__).parents[2]\n"
        'sys.path.insert(0, str(ROOT / "tests" / "helpers"))\nimport helper\n'
        'print("see README.md")\n',
    )
    write("tests/helpers/helper.py", 'import json\nDATA = ROOT / "tests" / "data" / "v.json"\nimport test_dep\n')
    write("tests/helpers/test_dep.py")
    write("tests/conftest.py")
    write("tests/data/v.json", "{}")
    write("tests/fixtures/a.bin")
    write("tests/ignored.py")
    write("tests/unrelated.py")
    write("README.md")
    files = boards_inputs.inputs(tmp_path)
    assert files == [
        ".github/workflows/ux-shot.yml",
        "build-aux/boards.sh",
        "build-aux/bounded.sh",
        "tests/conftest.py",
        "tests/data/v.json",
        "tests/helpers/helper.py",
        "tests/helpers/test_dep.py",
        "tests/ui/board.py",
    ]
    # A helper the filter misses fails the guard until it is listed.
    assert boards_inputs.missing(["tests/ui/**", "build-aux/*.sh"], files) == [
        "tests/conftest.py",
        "tests/data/v.json",
        "tests/helpers/helper.py",
        "tests/helpers/test_dep.py",
    ]


def test_boards_scan_follows_variable_rooted_and_joined_paths(tmp_path):
    # "$root/x" in shell and os.path.join(ROOT, "x") or Path(ROOT, "x") in
    # Python are this repository's usual idioms; the scan must read them.
    boards_inputs = module("boards_inputs")

    def write(path, text=""):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)

    write(".github/workflows/ux-shot.yml", "run: build-aux/boards.sh\n")
    write(
        "build-aux/boards.sh",
        'root=$(cd "$(dirname "$0")/.." && pwd)\npython3 "$root/tests/ui/board.py"\n"${root}/tests/run.sh"\n',
    )
    write("tests/run.sh")
    write(
        "tests/ui/board.py",
        "import os\nfrom pathlib import Path\nROOT = Path(__file__).parents[2]\n"
        'A = os.path.join(ROOT, "tests", "data", "a.json")\n'
        'B = Path(ROOT, "tests", "data", "b.json")\n'
        'C = (ROOT / "tests").joinpath("data", "c.json")\n'
        'D = os.path.join(str(ROOT), "tests", "fixtures")\n',
    )
    for name in ("a.json", "b.json", "c.json", "unrelated.json"):
        write(f"tests/data/{name}", "{}")
    write("tests/fixtures/f.bin")
    # A build that ran in the checkout: its products are named but are no source.
    write("build-aux/boards.sh", (tmp_path / "build-aux/boards.sh").read_text() + "cp _build-core/src/lib.so out/\n")
    write("_build-core/build.ninja")
    write("_build-core/src/lib.so")
    assert boards_inputs.inputs(tmp_path) == [
        ".github/workflows/ux-shot.yml",
        "build-aux/boards.sh",
        "tests/data/a.json",
        "tests/data/b.json",
        "tests/data/c.json",
        "tests/fixtures/f.bin",
        "tests/run.sh",
        "tests/ui/board.py",
    ]


def test_push_identical_pr_tree_skips_only_verified_green_desktop_areas(tmp_path):
    # Package builds run without git installed (check_pytest_ran.py SKIPS); use
    # deterministic git/gh stubs here so every package build runs this test too.
    changes = module("changes")
    base_sha = "1" * 40
    pr_sha = "2" * 40
    merge_sha = "3" * 40
    skew_sha = "4" * 40
    octopus_sha = "5" * 40
    same_tree = "a" * 40
    diff_tree = "b" * 40
    base_areas = dict.fromkeys(changes.AREAS, True)

    rev_list_map = {
        merge_sha: f"{merge_sha} {base_sha} {pr_sha}\n".encode(),
        skew_sha: f"{skew_sha} {base_sha} {pr_sha}\n".encode(),
        base_sha: f"{base_sha}\n".encode(),
        octopus_sha: f"{octopus_sha} {base_sha} {pr_sha} {'6' * 40}\n".encode(),
    }
    tree_map = {
        f"{merge_sha}^{{tree}}": f"{same_tree}\n".encode(),
        f"{pr_sha}^{{tree}}": f"{same_tree}\n".encode(),
        f"{skew_sha}^{{tree}}": f"{diff_tree}\n".encode(),
    }

    def stub_git(cmd):
        if cmd[:4] == ["git", "rev-list", "--parents", "-n"] and cmd[4] == "1":
            return rev_list_map[cmd[5]]
        if cmd[:2] == ["git", "rev-parse"]:
            return tree_map[cmd[2]]
        raise AssertionError(f"Unexpected git command: {cmd}")

    # 1. All 4 desktop areas green in the PR: skips recording, boards, offline, desktop; keeps source, packages, library.
    seen_calls = []

    def verify_all(repo_name, sha, expected_tree):
        seen_calls.append((repo_name, sha, expected_tree))
        return frozenset({"recording", "boards", "offline", "desktop"})

    areas_all, desktop_all, note_all = changes.apply_push_pr_tree_skips(
        "push",
        base_areas,
        repo="codemyriad/cassini-gnome",
        commit=merge_sha,
        git=stub_git,
        verifier=verify_all,
    )
    assert seen_calls == [("codemyriad/cassini-gnome", pr_sha, same_tree)]
    assert areas_all == {
        "source": True,
        "packages": True,
        "recording": False,
        "boards": False,
        "library": True,
        "offline": False,
    }
    assert desktop_all is False and "skipping recording, boards, offline, desktop" in note_all

    # Partial PR path-filter: PR only ran recording + desktop (skipped boards + offline) -> push runs boards + offline!
    areas_partial, desktop_partial, _ = changes.apply_push_pr_tree_skips(
        "push",
        base_areas,
        repo="codemyriad/cassini-gnome",
        commit=merge_sha,
        git=stub_git,
        verifier=lambda _r, _s, _t: frozenset({"recording", "desktop"}),
    )
    assert areas_partial["recording"] is False
    assert areas_partial["boards"] is True and areas_partial["offline"] is True
    assert areas_partial["source"] is True and areas_partial["packages"] is True and areas_partial["library"] is True
    assert desktop_partial is False

    # Flatpak enabled requires desktop_flatpak proof before skipping desktop.
    _, desktop_no_fp, _ = changes.apply_push_pr_tree_skips(
        "push",
        base_areas,
        repo="codemyriad/cassini-gnome",
        commit=merge_sha,
        flatpak_enabled=True,
        git=stub_git,
        verifier=lambda _r, _s, _t: frozenset({"desktop"}),
    )
    assert desktop_no_fp is True
    _, desktop_with_fp, _ = changes.apply_push_pr_tree_skips(
        "push",
        base_areas,
        repo="codemyriad/cassini-gnome",
        commit=merge_sha,
        flatpak_enabled=True,
        git=stub_git,
        verifier=lambda _r, _s, _t: frozenset({"desktop", "desktop_flatpak"}),
    )
    assert desktop_with_fp is False

    # API error fails open to running every desktop job.
    def fail_verifier(_r, _s, _t):
        raise RuntimeError("API down")

    areas_err, desktop_err, note_err = changes.apply_push_pr_tree_skips(
        "push", base_areas, repo="codemyriad/cassini-gnome", commit=merge_sha, git=stub_git, verifier=fail_verifier
    )
    assert all(areas_err.values()) and desktop_err is True and "running desktop jobs" in note_err

    # Non-push events, missing repo, and non-main refs never query git or verifier.
    for evt, repo_arg, ref in (
        ("pull_request", "codemyriad/cassini-gnome", "refs/heads/main"),
        ("schedule", "codemyriad/cassini-gnome", "refs/heads/main"),
        ("push", "", "refs/heads/main"),
        ("push", "codemyriad/cassini-gnome", "refs/heads/dev"),
    ):
        areas_noop, desktop_noop, note_noop = changes.apply_push_pr_tree_skips(
            evt,
            base_areas,
            repo=repo_arg,
            ref=ref,
            git=lambda _c: pytest.fail("Should not query git"),
            verifier=lambda _r, _s, _t: pytest.fail("Should not query verifier"),
        )
        assert all(areas_noop.values()) and desktop_noop is True and note_noop is None

    # 2. Merge skew, 1-parent commit, or 3-parent octopus merge -> never queries API, skips nothing.
    for rev in (skew_sha, base_sha, octopus_sha):
        areas_skew, desktop_skew, _ = changes.apply_push_pr_tree_skips(
            "push",
            base_areas,
            repo="codemyriad/cassini-gnome",
            commit=rev,
            git=stub_git,
            verifier=lambda _r, _s, _t: pytest.fail(
                "Verifier must not run on non-identical tree or non-2-parent commit"
            ),
        )
        assert all(areas_skew.values()) and desktop_skew is True

    # 3. End-to-end CLI invocation of changes.py on push with stubbed `git` and `gh` binaries.
    pr_merge_sha = "7" * 40
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"if args == ['rev-list', '--parents', '-n', '1', '{merge_sha}']:\n"
        f"    print('{merge_sha} {base_sha} {pr_sha}')\n"
        f"elif args in (['rev-parse', '{merge_sha}^{{tree}}'], ['rev-parse', '{pr_sha}^{{tree}}']):\n"
        f"    print('{same_tree}')\n"
        "else:\n"
        "    raise SystemExit(f'unexpected git call: {args}')\n"
    )
    fake_git.chmod(0o755)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "url = sys.argv[-1]\n"
        "if 'actions/workflows/ci.yml/runs' in url:\n"
        "    run = {\n"
        f"        'id': 77, 'event': 'pull_request', 'head_sha': '{pr_sha}',\n"
        "        'head_repository': {'full_name': 'codemyriad/cassini-gnome'},\n"
        "        'status': 'completed', 'conclusion': 'success',\n"
        "    }\n"
        "    if not os.environ.get('FAKE_OMIT_REF_WF'):\n"
        "        run['referenced_workflows'] = [{\n"
        f"            'path': 'codemyriad/cassini-gnome/.github/workflows/meson-tests.yml@{pr_merge_sha}',\n"
        "            'ref': 'refs/pull/77/merge',\n"
        f"            'sha': '{pr_merge_sha}',\n"
        "        }]\n"
        "    print(json.dumps({'workflow_runs': [run]}))\n"
        "elif 'actions/runs/77/jobs' in url:\n"
        "    print(json.dumps({'jobs': [\n"
        "        {'name': 'Build / Meson build profiles', 'status': 'completed', 'conclusion': 'success'},\n"
        "        {'name': 'Meson tests / Meson tests (minimal environment)', 'status': 'completed', 'conclusion': 'success'},\n"
        "        {'name': 'Recording / capture', 'status': 'completed', 'conclusion': 'success'},\n"
        "        {'name': 'Recording / live-folder', 'status': 'completed', 'conclusion': 'success'},\n"
        "        {'name': 'Boards', 'status': 'completed', 'conclusion': 'skipped'},\n"
        "        {'name': 'Offline / pinned-offline-recognition', 'status': 'completed', 'conclusion': 'success'},\n"
        "        {'name': 'Desktop / GNOME 50 / installed deb', 'status': 'completed', 'conclusion': 'success'},\n"
        "    ]}))\n"
        f"elif url == 'repos/codemyriad/cassini-gnome/commits/{pr_merge_sha}':\n"
        "    if os.environ.get('FAKE_COMMIT_404'):\n"
        "        sys.stderr.write('gh: Not Found (HTTP 404)\\n')\n"
        "        raise SystemExit(1)\n"
        "    print(json.dumps({\n"
        f"        'sha': '{pr_merge_sha}',\n"
        f"        'parents': [{{'sha': '{base_sha}'}}, {{'sha': '{pr_sha}'}}],\n"
        "        'commit': {'tree': {'sha': os.environ['FAKE_TESTED_TREE']}},\n"
        "    }))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected gh call: {sys.argv}')\n"
    )
    fake_gh.chmod(0o755)
    script_path = str(Path(__file__).parents[1] / "build-aux/ci/changes.py")

    def run_push_cli(tag, **extra_env):
        out_f = tmp_path / f"push-out-{tag}"
        rep_f = tmp_path / f"push-report-{tag}.json"
        sum_f = tmp_path / f"push-summary-{tag}.md"
        cli_env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        cli_env["PATH"] = f"{bin_dir}:{cli_env.get('PATH', '')}"
        cli_env["GITHUB_REPOSITORY"] = "codemyriad/cassini-gnome"
        cli_env["GITHUB_REF"] = "refs/heads/main"
        cli_env["GITHUB_STEP_SUMMARY"] = str(sum_f)
        cli_env.update(extra_env)
        subprocess.run(
            [
                sys.executable,
                script_path,
                "--event",
                "push",
                "--commit",
                merge_sha,
                "--output",
                str(out_f),
                "--report",
                str(rep_f),
            ],
            cwd=tmp_path,
            check=True,
            env=cli_env,
        )
        return json.loads(rep_f.read_text()), dict(line.split("=", 1) for line in out_f.read_text().splitlines())

    cli_report, emitted = run_push_cli("ok", FAKE_TESTED_TREE=same_tree)
    assert cli_report["areas"] == {
        "source": True,
        "packages": True,
        "recording": False,
        "boards": True,
        "library": True,
        "offline": False,
    }
    assert cli_report["desktop"] is False
    assert emitted["recording"] == "false" and emitted["offline"] == "false" and emitted["desktop"] == "false"
    assert emitted["boards"] == "true" and emitted["source"] == "true" and emitted["packages"] == "true"

    # Codex #415: identical PR head SHA whose selected PR run tested a different synthetic merge tree
    # (e.g. a PR targeting a release branch with extra fixes), or missing checkout-tree evidence,
    # MUST run every desktop job on main!
    for case_tag, env_overrides in (
        ("diff-tested-tree", {"FAKE_TESTED_TREE": diff_tree}),
        ("no-ref-wf", {"FAKE_TESTED_TREE": same_tree, "FAKE_OMIT_REF_WF": "1"}),
        ("commit-404", {"FAKE_TESTED_TREE": same_tree, "FAKE_COMMIT_404": "1"}),
    ):
        unverified_report, unverified_emitted = run_push_cli(case_tag, **env_overrides)
        assert all(unverified_report["areas"].values()), case_tag
        assert unverified_report["desktop"] is True, case_tag
        for desktop_key in ("recording", "boards", "offline", "desktop"):
            assert unverified_emitted[desktop_key] == "true", (case_tag, desktop_key)
