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
        ("tests/parakeet/queue_probe.py", {"boards", "offline"}),
        ("tests/diarization/queue_probe.py", {"boards", "offline"}),
        ("tests/test_diarization_models.py", {"boards", "offline"}),
        ("tests/ux/diarization/golden.json", {"boards", "library", "offline"}),
        ("tests/ui/quit.py", {"boards"}),
        ("tests/ui/future-board.py", {"boards"}),
        ("tests/test_model_download.py", {"boards"}),
        ("tests/desktop_services.py", {"boards"}),
        ("tests/recording/readiness.py", {"boards"}),
        ("tests/conftest.py", {"boards"}),
        ("tests/test_future_ui_dependency.py", {"boards"}),
        ("tests/new_helpers.py", {"boards"}),
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

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=CI fixture", "-c", "user.email=ci@example.invalid", *args], cwd=tmp_path, env=env
        )

    git("init", "--initial-branch=main")
    source = tmp_path / "src/cassini_talk/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("fixture\n")
    git("add", ".")
    git("commit", "-m", "Before")
    base = git("rev-parse", "HEAD").decode().strip()
    source.rename(tmp_path / "notes.txt")
    git("add", "-A")
    git("commit", "-m", "Moved outside area")
    head = git("rev-parse", "HEAD").decode().strip()
    payload = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    areas, _reason, paths = module("changes").detect(
        "pull_request", payload, git=lambda command: subprocess.check_output(command, cwd=tmp_path, env=env)
    )
    assert areas["library"] and areas["offline"] and areas["boards"]
    assert set(paths) == {"src/cassini_talk/library.py", "notes.txt"}


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
    assert all(json.loads(report.read_text())["areas"].values())
    assert len(output.read_text().splitlines()) == 6 and "false" not in output.read_text()


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
    # D20: a file Boards reads that its PR filter misses would let a PR
    # changing only that file skip Boards; build-aux/ci/boards_inputs.py
    # --why PATH says how the scan reached a file, --patterns tests/ prints
    # the globs.
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
