# SPDX-License-Identifier: GPL-3.0-or-later
"""PR coverage/fail-open history, and exact build-product identity across jobs."""

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
        ("src/cassini_viewer/player.py", {"boards", "library", "offline"}),
        ("src/cassini_viewer/library.py", {"boards", "library", "offline"}),
        ("src/cassini_viewer/parakeet.py", {"boards", "library", "offline"}),
        ("tests/parakeet/queue_probe.py", {"boards", "offline"}),
        ("tests/diarization/queue_probe.py", {"boards", "offline"}),
        ("tests/test_diarization_models.py", {"boards", "offline"}),
        ("tests/ux/diarization/golden.json", {"boards", "library", "offline"}),
        ("tests/ui/quit.py", {"boards"}),
        ("tests/test_model_download.py", {"boards"}),
        ("tests/desktop_services.py", {"boards"}),
        ("tests/test_future_ui_dependency.py", {"boards"}),
        ("tests/new_helpers.py", {"boards"}),
        ("tests/ux/states-vectors.json", {"boards", "library"}),
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
    source = tmp_path / "src/cassini_viewer/library.py"
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
    assert set(paths) == {"src/cassini_viewer/library.py", "notes.txt"}


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
        "src/cassini_viewer/model_download.py",
        "src/cassini_viewer/model_manifest.py",
        "src/cassini_viewer/zstd_stream.py",
        "src/cassini_viewer/strings.json",
        "src/cassini_viewer/portable_context.py",
        "src/cassini_viewer/meeting_store.py",
        "src/cassini_viewer/recording_session.py",
        "src/cassini_viewer/future_dependency.py",
        "src/cassini-viewer.gresource.xml",
        "build-aux/whistle-cache.sh",
    ],
)
def test_transitive_dependencies_run_both_engine_and_library_checks(path):
    selected = module("changes").select([path])
    assert selected["library"] and selected["offline"]
