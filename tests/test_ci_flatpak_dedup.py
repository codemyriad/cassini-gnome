# SPDX-License-Identifier: GPL-3.0-or-later
"""F: the Flatpak job builds the host Trash portal once per input (a main-saved cache)."""

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FLATPAK = ROOT / ".github/workflows/flatpak.yml"
SCRIPT = ROOT / "tests/flatpak/prepare_trash_portal.py"
MAIN_ONLY = "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"


def steps():
    return re.split(r"\n(?=      - )", FLATPAK.read_text())[1:]


def field(step, name):
    match = re.search(rf"^\s+{name}: (.+)$", step, re.M)
    return match and match[1].strip()


def index(predicate):
    (found,) = [i for i, step in enumerate(steps()) if predicate(step)]
    return found


def test_the_trash_portal_is_restored_built_and_saved_from_main_only_at_one_path():
    all_steps = steps()
    restore = index(lambda s: "uses: actions/cache/restore@" in s and "_ci-trash-portal" in s)
    prepare = index(lambda s: "prepare_trash_portal.py --output" in s)
    save = index(lambda s: "uses: actions/cache/save@" in s and "_ci-trash-portal" in s)
    devel = index(lambda s: "smoke.sh cassini-talk-devel.flatpak" in s)
    assert restore + 1 == prepare and prepare + 1 == save and save < devel
    assert field(all_steps[restore], "id") == "trash-portal"
    # The same tree everywhere: restored, built (or kept) and saved at the path
    # prepare_trash_portal.py writes into its marker and rpath, then used by smoke.sh.
    assert field(all_steps[restore], "path") == field(all_steps[save], "path") == "_ci-trash-portal"
    assert 'prepare_trash_portal.py --output "$PWD/_ci-trash-portal"' in all_steps[prepare]
    assert 'portal_prefix="$PWD/_ci-trash-portal"' in (ROOT / "build-aux/flatpak/smoke.sh").read_text()
    # The prepare step always runs: a hit is used only if its marker matches.
    assert field(all_steps[prepare], "if") is None
    assert field(all_steps[save], "if") == MAIN_ONLY + " && steps.trash-portal.outputs.cache-hit != 'true'"
    assert field(all_steps[save], "key") == "${{ steps.trash-portal.outputs.cache-primary-key }}"
    assert not any("uses: actions/cache@" in step and "_ci-trash-portal" in step for step in all_steps)


def test_the_trash_portal_key_names_every_input_of_its_build():
    key = field(steps()[index(lambda s: "id: trash-portal" in s)], "key")
    digest = re.search(r"^      image: \S+@sha256:([0-9a-f]{64})$", FLATPAK.read_text(), re.M)[1]
    # The script holds the pins and build flags; the container image is the SDK
    # and toolchain it compiles against; the runner's arch and kind come last.
    assert key == (
        "trash-portal-${{ runner.arch }}-${{ runner.environment }}-"
        + digest[:16]
        + "-${{ hashFiles('tests/flatpak/prepare_trash_portal.py') }}"
    )
    # Hashing the script alone covers its inputs: it reads no other file of the
    # repository, and every download is pinned by version and SHA-256 inside it.
    text = SCRIPT.read_text()
    assert not re.search(r"build-aux|meson\.build|tests/|src/", text)
    for pin in ("VERSION", "SHA256", "FUSE_VERSION", "FUSE_SHA256"):
        assert re.search(rf"^{pin} = \"[0-9a-f.]+\"$", text, re.M), pin


@pytest.fixture
def portal(monkeypatch, tmp_path):
    """prepare_trash_portal with the downloads and meson replaced: each meson
    install writes what the real one installs that the smoke uses."""
    spec = importlib.util.spec_from_file_location("prepare_trash_portal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archives = {}
    for name, top in (("fuse", f"fuse-{module.FUSE_VERSION}"), ("portal", f"xdg-desktop-portal-{module.VERSION}")):
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(f"{top}/meson.build")
            data = b"project('x')\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        archives[name] = archive
    monkeypatch.setattr(
        module, "fetch", lambda cache, name, url, digest: archives["fuse" if "fuse" in name else "portal"]
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command[:2])
        if command[:2] == ["meson", "install"]:
            prefix = calls_prefix[0]
            if Path(command[3]).name == "fuse-build":
                (prefix / "lib").mkdir(parents=True, exist_ok=True)
                (prefix / "lib/libfuse3.so.3.18.3").write_bytes(b"\x7fELF fuse")
                (prefix / "lib/libfuse3.so.4").unlink(missing_ok=True)
                (prefix / "lib/libfuse3.so.4").symlink_to("libfuse3.so.3.18.3")
            else:
                (prefix / "libexec").mkdir(parents=True, exist_ok=True)
                (prefix / "libexec/xdg-desktop-portal").write_bytes(b"\x7fELF portal")
                (prefix / "libexec/xdg-desktop-portal").chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    calls_prefix = []
    monkeypatch.setattr(module.subprocess, "run", run)

    def prepare(output):
        calls_prefix[:] = [output.resolve()]
        calls.clear()
        module.prepare(output, tmp_path / "downloads")
        return list(calls)

    return module, prepare


def snapshot(tree):
    """Every entry under tree: its type, mode, bytes or link target."""
    out = {}
    for path in sorted(tree.rglob("*")):
        relative = str(path.relative_to(tree))
        if path.is_symlink():
            out[relative] = ("link", os.readlink(path))
        elif path.is_file():
            out[relative] = ("file", path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            out[relative] = ("dir", path.stat().st_mode)
    return out


def cache_round_trip(tree):
    """What actions/cache does between main's save and a later restore: tar the
    path, drop the workspace, untar it at the same place."""
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as tar:
        tar.add(tree, arcname=tree.name)
    for path in sorted(tree.rglob("*"), reverse=True):
        path.unlink() if path.is_symlink() or path.is_file() else path.rmdir()
    tree.rmdir()
    packed.seek(0)
    with tarfile.open(fileobj=packed) as tar:
        tar.extractall(tree.parent, filter="tar")


def test_a_restored_portal_is_used_as_built_and_anything_else_is_rebuilt(portal, tmp_path):
    module, prepare = portal
    workspace = tmp_path / "workspace"
    output = workspace / "_ci-trash-portal"
    builds = [["meson", "setup"], ["meson", "compile"], ["meson", "install"]] * 2
    assert prepare(output) == builds  # a cache miss: nothing there, so it builds
    marker = json.loads((output / "cassini-test-tool.json").read_text())
    assert marker["prefix"] == str(output.resolve())
    built = snapshot(output)
    assert {"libexec/xdg-desktop-portal", "lib/libfuse3.so.4", "cassini-test-tool.json"} <= set(built)
    # A hit: the same tree at the same path, and prepare runs no build at all.
    cache_round_trip(output)
    assert snapshot(output) == built
    assert prepare(output) == []
    assert snapshot(output) == built
    # The same tree at another prefix (its rpath would point elsewhere) is rebuilt.
    elsewhere = tmp_path / "other-workspace/_ci-trash-portal"
    elsewhere.parent.mkdir()
    output.rename(elsewhere)
    assert prepare(elsewhere) == builds
    # So is one whose marker names other pins, or that lost the portal.
    for change in ("pins", "binary"):
        cache_round_trip(elsewhere)
        if change == "pins":
            stale = dict(json.loads((elsewhere / "cassini-test-tool.json").read_text()), version="0.0.0")
            (elsewhere / "cassini-test-tool.json").write_text(json.dumps(stale))
        else:
            (elsewhere / "libexec/xdg-desktop-portal").unlink()
        assert prepare(elsewhere) == builds, change
