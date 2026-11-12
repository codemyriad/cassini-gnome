# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for @sha256-pinned GHCR prebuilt per-distro CI images (GNOME-CI-CACHING.md #2)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_DIR = ROOT / "build-aux/ci"


def _load_module(name: str, path: Path):
    if str(CI_DIR) not in sys.path:
        sys.path.insert(0, str(CI_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ci_images = _load_module("ci_images", CI_DIR / "ci_images.py")
pin_images = _load_module("pin_images", CI_DIR / "pin_images.py")
check_pytest_ran = _load_module("check_pytest_ran_prebuilt", CI_DIR / "check_pytest_ran.py")


def _copy_prebuilt_Fixture(tmp_path: Path) -> Path:
    to_copy = {
        "build-aux/ci/image-pins.json",
        *ci_images.PREBUILT_INPUT_FILES,
    }
    for rel in sorted(to_copy):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst)
    return tmp_path


def test_prebuilt_specs_and_ci_images_workflow_cover_every_declared_input():
    assert len(ci_images.PREBUILT_SPECS) == 13
    seen_keys = set()
    for spec in ci_images.PREBUILT_SPECS:
        assert spec.key not in seen_keys
        seen_keys.add(spec.key)
        assert spec.repository == f"ghcr.io/codemyriad/cassini-gnome/ci-{spec.key}"
        assert spec.base in pin_images.IMAGES
        assert (ROOT / spec.dockerfile).is_file(), spec.dockerfile
        assert (ROOT / spec.context).is_dir(), spec.context
        assert spec.marker.startswith("/etc/")
        for rel in spec.inputs:
            assert (ROOT / rel).is_file(), (spec.key, rel)

    wf_text = (ROOT / ".github/workflows/ci-images.yml").read_text()
    for rel in ci_images.PREBUILT_INPUT_FILES:
        assert f"      - {rel}\n" in wf_text, f"ci-images.yml push.paths missing {rel}"

    matrix_out = json.loads(
        subprocess.check_output([sys.executable, str(CI_DIR / "ci_images.py"), "matrix"], text=True).strip()
    )
    assert [item["key"] for item in matrix_out["include"]] == [s.key for s in ci_images.PREBUILT_SPECS]


def test_input_hash_tracks_all_dependency_files_and_ignores_daily_version_stamps(tmp_path):
    repo = _copy_prebuilt_Fixture(tmp_path)
    pins = json.loads((repo / "build-aux/ci/image-pins.json").read_text())
    baseline = {
        spec.key: ci_images.compute_input_hash(spec, pins["images"][spec.base]["digest"], root=repo)
        for spec in ci_images.PREBUILT_SPECS
    }
    assert all(ci_images.INPUT_HASH_RE.fullmatch(h) for h in baseline.values())

    # Version stamps rewritten by daily_test_release.py apply must NOT invalidate RPM or Arch prebuilt images.
    spec_file = repo / "build-aux/rpm/cassini-talk.spec"
    orig_spec = spec_file.read_text()
    spec_file.write_text(re.sub(r"^Version:\s*\S+", "Version:        0.4.2~20261112.1", orig_spec, count=1, flags=re.M))
    assert (
        ci_images.compute_input_hash(
            ci_images.SPECS_BY_KEY["rpm-fedora-44"], pins["images"]["fedora:44"]["digest"], root=repo
        )
        == baseline["rpm-fedora-44"]
    )
    # Any BuildRequires change in cassini-talk.spec MUST invalidate both RPM prebuilt images.
    spec_file.write_text(orig_spec + "\nBuildRequires:  extra-test-dep\n")
    assert (
        ci_images.compute_input_hash(
            ci_images.SPECS_BY_KEY["rpm-fedora-44"], pins["images"]["fedora:44"]["digest"], root=repo
        )
        != baseline["rpm-fedora-44"]
    )
    assert (
        ci_images.compute_input_hash(
            ci_images.SPECS_BY_KEY["rpm-opensuse-tumbleweed"],
            pins["images"]["registry.opensuse.org/opensuse/tumbleweed:latest"]["digest"],
            root=repo,
        )
        != baseline["rpm-opensuse-tumbleweed"]
    )
    spec_file.write_text(orig_spec)

    pkgbuild = repo / "build-aux/arch/PKGBUILD"
    orig_pkgbuild = pkgbuild.read_text()
    pkgbuild.write_text(re.sub(r"^pkgver=\S+", "pkgver=0.4.2.20261112.1", orig_pkgbuild, count=1, flags=re.M))
    assert (
        ci_images.compute_input_hash(
            ci_images.SPECS_BY_KEY["arch-archlinux"], pins["images"]["archlinux:latest"]["digest"], root=repo
        )
        == baseline["arch-archlinux"]
    )
    # Changing depends/makedepends/checkdepends in PKGBUILD MUST invalidate arch-archlinux.
    pkgbuild.write_text(orig_pkgbuild.replace("makedepends=(meson gcc)", "makedepends=(meson)"))
    assert (
        ci_images.compute_input_hash(
            ci_images.SPECS_BY_KEY["arch-archlinux"], pins["images"]["archlinux:latest"]["digest"], root=repo
        )
        != baseline["arch-archlinux"]
    )
    pkgbuild.write_text(orig_pkgbuild)

    # Every declared input file changes the hash of every spec that lists it, and removing a dep changes it too.
    for rel in ci_images.PREBUILT_INPUT_FILES:
        target = repo / rel
        original = target.read_text()
        target.write_text(original + "\n# dependency-mutation\n")
        changed = {
            spec.key
            for spec in ci_images.PREBUILT_SPECS
            if ci_images.compute_input_hash(spec, pins["images"][spec.base]["digest"], root=repo) != baseline[spec.key]
        }
        expected = {spec.key for spec in ci_images.PREBUILT_SPECS if rel in spec.inputs}
        assert changed == expected, rel
        target.write_text(original)

    # Changing the pinned base digest also invalidates the prebuilt hash.
    alt_digest = "sha256:" + ("f" * 64)
    assert (
        ci_images.compute_input_hash(ci_images.SPECS_BY_KEY["ubuntu-build"], alt_digest, root=repo)
        != baseline["ubuntu-build"]
    )


def test_resolve_prebuilt_enforces_digest_pin_input_hash_and_unpinned_nightly_bypass(tmp_path):
    repo = _copy_prebuilt_Fixture(tmp_path)
    pins_path = repo / "build-aux/ci/image-pins.json"
    pins = json.loads(pins_path.read_text())
    ubuntu_ref = f"ubuntu:26.04@{pins['images']['ubuntu:26.04']['digest']}"

    # 1. Initial bootstrap (digest is null) resolves to None so jobs build from raw pinned base.
    assert ci_images.resolve_prebuilt("deb", ubuntu_ref, root=repo, env={}) is None

    # 2. Populate synthetic GHCR digests via sync_prebuilt_repo; pinned refs now resolve to @sha256 GHCR digests.
    def fake_ghcr(tag_ref: str) -> str:
        return "sha256:" + (f"{abs(hash(tag_ref)):064x}"[:64])

    pin_images.sync_prebuilt_repo(root=repo, prebuilt_resolver=fake_ghcr)
    synced = json.loads(pins_path.read_text())
    deb_digest = synced["prebuilt_images"]["deb-ubuntu-26.04"]["digest"]
    assert ci_images.resolve_prebuilt("deb", ubuntu_ref, root=repo, env={}) == (
        f"ghcr.io/codemyriad/cassini-gnome/ci-deb-ubuntu-26.04@{deb_digest}"
    )

    # 3. Unpinned rolling tags (nightly schedule and rpm-canary) NEVER resolve to prebuilt images,
    #    even when prebuilt_images has valid published digests.
    for role, unpinned in (
        ("source", "debian:testing"),
        ("source", "fedora:latest"),
        ("source", "registry.opensuse.org/opensuse/tumbleweed:latest"),
        ("source", "archlinux:latest"),
        ("source", "alpine:edge"),
        ("deb", "debian:testing"),
        ("rpm", "registry.opensuse.org/opensuse/tumbleweed:latest"),
        ("rpm", "fedora:latest"),
        ("arch", "archlinux:latest"),
    ):
        assert ci_images.resolve_prebuilt(role, unpinned, root=repo, env={}) is None, (role, unpinned)

    # 4. Opt-out env var forces raw base build.
    assert ci_images.resolve_prebuilt("deb", ubuntu_ref, root=repo, env={"CASSINI_NO_PREBUILT_CI_IMAGES": "1"}) is None

    # 5. Modifying or removing a build dependency in a PR invalidates input_hash -> returns None (local rebuild!).
    ctrl = repo / "debian/control"
    orig_ctrl = ctrl.read_text()
    ctrl.write_text(orig_ctrl.replace("               libgtk-4-bin,\n", ""))
    assert ci_images.resolve_prebuilt("deb", ubuntu_ref, root=repo, env={}) is None
    # Unrelated role (ubuntu-build) still hits its prebuilt GHCR digest.
    assert ci_images.resolve_prebuilt("ubuntu-build", ubuntu_ref, root=repo, env={}) is not None
    ctrl.write_text(orig_ctrl)

    # 6. Base digest mismatch returns None.
    wrong_ubuntu = "ubuntu:26.04@sha256:" + ("0" * 64)
    assert ci_images.resolve_prebuilt("deb", wrong_ubuntu, root=repo, env={}) is None


def test_pull_and_prepare_isolate_ghcr_auth_and_fall_back_to_local_build(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_images.ci_image_core.time, "sleep", lambda _s: None)
    repo = _copy_prebuilt_Fixture(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    pins = json.loads((repo / "build-aux/ci/image-pins.json").read_text())
    ubuntu_ref = f"ubuntu:26.04@{pins['images']['ubuntu:26.04']['digest']}"
    fake_digest = "sha256:" + ("a" * 64)
    pin_images.sync_prebuilt_repo(root=repo, prebuilt_resolver=lambda _t: fake_digest)
    expected_ghcr = f"ghcr.io/codemyriad/cassini-gnome/ci-ubuntu-build@{fake_digest}"

    # 1. Pull hit with GHCR_READ_TOKEN uses a disposable DOCKER_CONFIG/REGISTRY_AUTH_FILE and never writes ~/.docker.
    seen_calls: list[tuple[list[str], dict[str, str] | None]] = []

    def runner_hit(cmd, **kwargs):
        env_arg = kwargs.get("env")
        seen_calls.append((list(cmd), env_arg))
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=1)
        if cmd[1] == "login":
            assert env_arg is not None
            cfg_dir = Path(env_arg["DOCKER_CONFIG"])
            assert cfg_dir.is_dir() and cfg_dir != fake_home / ".docker"
            assert env_arg["REGISTRY_AUTH_FILE"] == str(cfg_dir / "config.json")
            (cfg_dir / "config.json").write_text('{"auths":{"ghcr.io":{}}}')
            return SimpleNamespace(returncode=0)
        if cmd[1] == "pull":
            assert kwargs.get("stdout") is sys.stderr
            assert env_arg is not None and Path(env_arg["DOCKER_CONFIG"]).is_dir()
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    env = {
        "CONTAINER_ENGINE": "docker",
        "GHCR_READ_TOKEN": "ghs_ephemeral_token",
        "GITHUB_ACTOR": "cassini-bot",
    }
    prepared = ci_images.prepare_image("ubuntu-build", ubuntu_ref, root=repo, env=env, runner=runner_hit)
    assert prepared == expected_ghcr
    assert not (fake_home / ".docker").exists()
    # Disposable auth dir was removed after context exit.
    login_env = next(e for c, e in seen_calls if c[1] == "login")
    assert login_env is not None and not Path(login_env["DOCKER_CONFIG"]).exists()

    # 2. GHCR pull failure falls back to pulling the @sha256 raw base and building locally.
    fallback_cmds: list[list[str]] = []

    def runner_fallback(cmd, **kwargs):
        fallback_cmds.append(list(cmd))
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=1)
        if cmd[1] == "pull" and cmd[-1] == expected_ghcr:
            assert kwargs.get("stdout") is sys.stderr
            return SimpleNamespace(returncode=1)
        if cmd[1] == "pull" and cmd[-1] == ubuntu_ref:
            assert kwargs.get("stdout") is sys.stderr
            return SimpleNamespace(returncode=0)
        if cmd[1] == "build":
            assert kwargs.get("stdout") is sys.stderr
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0)

    local_tag = ci_images.prepare_image(
        "ubuntu-build",
        ubuntu_ref,
        root=repo,
        env={"CONTAINER_ENGINE": "docker"},
        runner=runner_fallback,
    )
    assert local_tag == "cassini-ci-local:ubuntu-build"
    assert any(c[1] == "build" and f"BASE={ubuntu_ref}" in c for c in fallback_cmds)

    # 3. Real CLI subprocesses never leak `docker pull -q` / `docker build` stdout into $GITHUB_ENV or $(...).
    for script in ("ci_image_core.py", "ci_images.py", "prepare_build_image.py"):
        shutil.copy(CI_DIR / script, repo / "build-aux/ci" / script)
    stub_docker = tmp_path / "stub-docker"
    stub_docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then exit 1; fi\n'
        'if [ "$1" = "pull" ]; then\n'
        '  shift; [ "${1:-}" = "-q" ] && shift\n'
        '  [ "${FAIL_GHCR:-0}" = "1" ] && case "$1" in ghcr.io/*) exit 1 ;; esac\n'
        '  echo "docker.io/library/$1"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "build" ]; then echo "building noisy stdout"; exit 0; fi\n'
        "exit 0\n"
    )
    stub_docker.chmod(0o755)
    cli_env = {**os.environ, "CONTAINER_ENGINE": str(stub_docker)}
    cli_env.pop("GHCR_READ_TOKEN", None)
    cli_env.pop("GITHUB_TOKEN", None)

    hit_proc = subprocess.run(
        [sys.executable, str(repo / "build-aux/ci/ci_images.py"), "pull", "ubuntu-build", ubuntu_ref],
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert hit_proc.stdout == f"{expected_ghcr}\n"
    assert "docker.io/library/" in hit_proc.stderr

    fb_proc = subprocess.run(
        [sys.executable, str(repo / "build-aux/ci/prepare_build_image.py"), ubuntu_ref],
        env={**cli_env, "FAIL_GHCR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert fb_proc.stdout == "cassini-ci-local:ubuntu-build\n"
    assert "docker.io/library/" in fb_proc.stderr

    # 4. Unpinned base reference is rejected by prepare_image.
    with pytest.raises(ValueError, match="requires a @sha256-pinned base"):
        ci_images.prepare_image("ubuntu-build", "ubuntu:26.04", root=repo, env={"CONTAINER_ENGINE": "docker"})


def test_push_policy_blocks_pull_requests_and_non_main_refs():
    for event in ("push", "schedule", "workflow_dispatch"):
        ci_images.enforce_push_policy({"GITHUB_EVENT_NAME": event, "GITHUB_REF": "refs/heads/main"})

    for bad_env in (
        {},
        {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/heads/main"},
        {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_REF": "refs/heads/main"},
        {"GITHUB_EVENT_NAME": "merge_group", "GITHUB_REF": "refs/heads/main"},
        {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/feature"},
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/tags/v0.4.2"},
    ):
        with pytest.raises(RuntimeError, match="GHCR image pushes are restricted"):
            ci_images.enforce_push_policy(bad_env)


def test_clean_install_smoke_stays_on_bare_base_across_all_package_scripts():
    container_sh = (ROOT / "build-aux/container.sh").read_text()
    assert "ci_images.py" not in container_sh
    for rel in ("build-aux/deb/build.sh", "build-aux/rpm/build.sh", "build-aux/arch/build.sh"):
        text = (ROOT / rel).read_text()
        smoke_calls = re.findall(r"^container_offline_smoke\s+(\S+)", text, re.M)
        assert smoke_calls == ['"$BASE"'], (rel, smoke_calls)
        assert '\ncontainer_pull "$BASE"\n' in text, rel


def test_alpine_prebuilt_outcome_and_run_in_build_image_shell_paths(tmp_path, monkeypatch):
    # 1. Exercise check-distros.sh's prebuilt Alpine outcome path with a stub engine and verify check_pytest_ran.source().
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_engine = bin_dir / "stub-engine"
    stub_engine.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'case "${1:-}" in\n'
        "  run)\n"
        '    case "$*" in\n'
        '      *"test -f /etc/cassini-deps-installed"*)\n'
        '        printf "ID=alpine\\nVERSION_ID=3.23.0_alpha\\n"\n'
        "        exit 0\n"
        "        ;;\n"
        "      *)\n"
        '        for arg in "$@"; do\n'
        '          case "$arg" in *:/logs:rw*) logs_dir=${arg%%:/logs:rw*} ;; esac\n'
        "        done\n"
        "        logs_dir=${logs_dir#-v }\n"
        '        if [ -n "${logs_dir:-}" ] && [ -d "$logs_dir" ]; then\n'
        '          printf "ID=alpine\\nVERSION_ID=3.23.0_alpha\\n" > "$logs_dir/os-release"\n'
        "        fi\n"
        '        printf "\\n==> meson setup\\n\\n==> runtime smoke test\\n@@versions python 3.13, gtk 4.18.0, adw 1.8.0, gst 1.26.0\\n\\n==> ok\\n"\n'
        "        exit 0\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  image|pull|rm|rmi|commit)\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        'printf "%s\\n" "$@"\n'
    )
    stub_engine.chmod(0o755)

    # Stub python3 ahead of PATH ONLY for the check-distros.sh invocation so ci_images.py pull returns a prebuilt ref.
    wrap_bin = tmp_path / "wrap-bin"
    wrap_bin.mkdir()
    stub_py = wrap_bin / "python3"
    stub_py.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "build-aux/ci/ci_images.py" ] && [ "${2:-}" = "pull" ]; then\n'
        '  echo "ghcr.io/codemyriad/cassini-gnome/ci-source-alpine-edge@sha256:' + ("c" * 64) + '"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    stub_py.chmod(0o755)

    outcome_root = tmp_path / "install-outcome"
    meson_logs_root = tmp_path / "meson-logs"
    alpine_pinned = (
        "alpine:edge@"
        + json.loads((ROOT / "build-aux/ci/image-pins.json").read_text())["images"]["alpine:edge"]["digest"]
    )
    res = subprocess.run(
        ["sh", "build-aux/check-distros.sh", alpine_pinned],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{wrap_bin}:{os.environ.get('PATH', '')}",
            "CONTAINER_ENGINE": str(stub_engine),
            "CASSINI_INSTALL_OUTCOME": str(outcome_root),
            "CASSINI_TEST_LOGS": str(meson_logs_root),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    alpine_outcome = outcome_root / "distro-alpine-edge"
    assert json.loads((alpine_outcome / "outcome.json").read_text()) == {
        "image": "alpine:edge",
        "step": "install-deps",
        "status": "installed",
    }
    assert "ID=alpine" in (alpine_outcome / "os-release").read_text()
    assert (alpine_outcome.stat().st_mode & 0o777) == 0o555
    assert ((alpine_outcome / "outcome.json").stat().st_mode & 0o777) == 0o444

    # Feed the check-distros.sh outcome directory into check_pytest_ran.source() with a complete 4-shard log fixture.
    logs_dir = meson_logs_root / "distro-alpine-edge.1"
    shards = 4
    fake_digest = "0" * 64
    monkeypatch.setattr(
        check_pytest_ran.shard_plan,
        "test_files",
        lambda *_a, **_kw: ["test_a.py"],
    )
    monkeypatch.setattr(
        check_pytest_ran.shard_plan,
        "assign",
        lambda *_a, **_kw: {"test_a.py": 1},
    )
    monkeypatch.setattr(check_pytest_ran.shard_plan, "tests_digest", lambda *_a, **_kw: fake_digest)
    entries = []
    for idx in range(1, shards + 1):
        cases = (
            '<testcase classname="tests.test_a" name="test_0" file="tests/test_a.py"></testcase>' if idx == 1 else ""
        )
        (logs_dir / f"pytest-{idx}.junit.xml").write_text(
            f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'
        )
        (logs_dir / f"pytest-{idx}.run.json").write_text(
            json.dumps({"tests": fake_digest, "inipath": "/build/src/pyproject.toml", "deselected": []})
        )
        entries.append(
            {
                "name": f"cassini-talk:pytest-{idx}",
                "result": "OK",
                "command": [
                    "/usr/bin/python3",
                    *check_pytest_ran.ARGUMENTS,
                    f"--junitxml=/build/b/meson-logs/pytest-{idx}.junit.xml",
                    "/build/src/tests",
                ],
                "env": {
                    "CASSINI_TEST_SHARD": f"{idx}/{shards}",
                    "CASSINI_TEST_REQUIRE_GTK": "1",
                    "CASSINI_TEST_TIMINGS": "1",
                },
            }
        )
    (logs_dir / "testlog.json").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    export_dir = tmp_path / "exported"
    kind, slug = check_pytest_ran.source(
        meson_logs_root,
        "alpine-edge",
        shards,
        export_dir,
        install_outcome=outcome_root,
    )
    assert (kind, slug) == ("nodes", "alpine-edge")

    # 2. Verify run-in-build-image.sh forwards UID/GID and env vars cleanly via a stub engine.
    stub_docker = bin_dir / "fake-docker"
    stub_docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub_docker.chmod(0o755)
    out = subprocess.check_output(
        [str(CI_DIR / "run-in-build-image.sh"), "meson", "compile", "-C", "_build-core"],
        cwd=tmp_path,
        env={
            **os.environ,
            "CONTAINER_ENGINE": str(stub_docker),
            "CASSINI_BUILD_IMAGE": "ghcr.io/codemyriad/cassini-gnome/ci-ubuntu-build@sha256:" + ("b" * 64),
            "PYTEST_ADDOPTS": "-vv",
            "CASSINI_PERF_GATE": "1",
            "GITHUB_SHA": "deadbeef",
        },
        text=True,
    ).splitlines()
    assert out[:4] == ["run", "--rm", "--network", "host"]
    assert "HOST_UID=0" in " ".join(out)  # non-'docker' engine name keeps 0:0 rootless mapping
    assert "PYTEST_ADDOPTS=-vv" in out
    assert "CASSINI_PERF_GATE=1" in out
    assert "GITHUB_SHA=deadbeef" in out
    assert out[-4:] == ["meson", "compile", "-C", "_build-core"]
