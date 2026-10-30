# SPDX-License-Identifier: GPL-3.0-or-later
"""D39 real filesystem/host transactions; never open or edit browser profiles."""

import json
import os
import shlex
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cassini_viewer import companion_bundle as bundle

ROOT = Path(__file__).resolve().parents[1]


def source(tmp_path, version="0.2.2"):
    root = tmp_path / ("bundle-" + version)
    root.mkdir()
    manifest = json.loads((ROOT / "browser-extension/manifest.json").read_text())
    manifest["version"] = version
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "app.js").write_text(f"console.log({version!r})")
    (root / "icons").mkdir()
    (root / "icons/logo.png").write_bytes(b"\x89PNG\x00\xff")
    return root


def fingerprint(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def native(monkeypatch):
    monkeypatch.setattr(bundle, "in_flatpak", lambda: False)


@pytest.mark.parametrize("data_name", ["Data with $ shell spaces", "Data 'quote' with \\ slash\nline"])
def test_install_is_stable_idempotent_and_newer_only_without_touching_profiles(tmp_path, data_name):
    original = source(tmp_path)
    target = tmp_path / data_name / "cassini/browser-companion"
    profile = tmp_path / "Browser/Default/Preferences"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"USER PREFERENCES UNTOUCHED")
    assert bundle.prepare(original, destination=target) == target
    first = fingerprint(target)
    bundle.prepare(original, destination=target)
    assert fingerprint(target) == first
    newer = source(tmp_path, "0.2.3")
    bundle.prepare(newer, destination=target)
    assert json.loads((target / "manifest.json").read_text())["version"] == "0.2.3"
    assert (target / "app.js").read_bytes() == (newer / "app.js").read_bytes()
    updated = fingerprint(target)
    bundle.prepare(original, destination=target)
    assert fingerprint(target) == updated
    assert profile.read_bytes() == b"USER PREFERENCES UNTOUCHED"
    assert not list(target.parent.glob(".browser-companion*"))


def test_pinned_id_and_browser_version_comparison():
    assert bundle.version(dict(version="0.2.2")) == bundle.version(dict(version="0.2.2.0"))
    assert bundle.version(dict(version="0.10.0")) > bundle.version(dict(version="0.2.9"))
    for value in (None, "", "1.2.3.4.5", "-1", "1.65536", "future"):
        with pytest.raises(ValueError):
            bundle.version(dict(version=value))
    with pytest.raises(ValueError):
        bundle.identity(dict(key="incorrect"))


@pytest.mark.parametrize("unsafe", ["symlink", "private-key", "fifo", "terminator"])
def test_unsafe_resource_never_gets_copied(tmp_path, unsafe):
    root = source(tmp_path)
    target = tmp_path / "destination"
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("PRIVATE_SENTINEL")
        (root / "link.txt").symlink_to(outside)
    elif unsafe == "private-key":
        (root / "secret.PEM").write_text("PRIVATE_SENTINEL")
    elif unsafe == "terminator":
        (root / "DONE").write_text("ambiguous transfer sentinel")
    else:
        os.mkfifo(root / "fifo")
    with pytest.raises(ValueError):
        bundle.prepare(root, destination=target)
    assert not target.exists()


def test_destination_symlink_is_refused_and_outside_is_unchanged(tmp_path):
    root = source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    target = tmp_path / "destination"
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        bundle.prepare(root, destination=target)
    assert (outside / "keep").read_text() == "untouched" and target.is_symlink()


def test_partial_stage_failure_keeps_previous_copy_and_removes_temporary_files(tmp_path, monkeypatch):
    old, new = source(tmp_path), source(tmp_path, "0.2.3")
    target = tmp_path / "destination"
    bundle.prepare(old, destination=target)
    before = fingerprint(target)
    run = bundle._run

    def corrupt(operation, destination, expected="missing", payload=None):
        if operation == "publish":
            lines = payload.splitlines()
            lines[1] = b"INVALID!"
            payload = b"\n".join(lines) + b"\n"
        return run(operation, destination, expected, payload)

    monkeypatch.setattr(bundle, "_run", corrupt)
    with pytest.raises(OSError):
        bundle.prepare(new, destination=target)
    assert fingerprint(target) == before
    assert not list(target.parent.glob(".browser-companion*"))


def test_failed_publish_rename_rolls_back_and_next_start_recovers_a_crash(tmp_path, monkeypatch):
    old, new = source(tmp_path), source(tmp_path, "0.2.3")
    target = tmp_path / "destination"
    bundle.prepare(old, destination=target)
    before = fingerprint(target)
    commands = tmp_path / "commands"
    commands.mkdir()
    real_mv = shutil.which("mv")
    script = commands / "mv"
    script.write_text('#!/bin/sh\ncase "$3" in */.browser-companion-*) exit 1 ;; esac\nexec ' + real_mv + ' "$@"\n')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.defpath)
    with pytest.raises(OSError):
        bundle.prepare(new, destination=target)
    assert fingerprint(target) == before
    previous = target.parent / ".browser-companion.previous"
    target.rename(previous)
    assert bundle.prepare(old, destination=target) == target
    assert fingerprint(target) == before and not previous.exists()


def test_concurrent_versions_cannot_downgrade_the_copy(tmp_path, monkeypatch):
    old, low, high = source(tmp_path), source(tmp_path, "0.2.3"), source(tmp_path, "0.2.4")
    target = tmp_path / "destination"
    bundle.prepare(old, destination=target)
    barrier, local, run = threading.Barrier(2), threading.local(), bundle._run

    def synchronize(operation, destination, expected="missing", payload=None):
        result = run(operation, destination, expected, payload)
        if operation == "inspect" and not getattr(local, "observed", False):
            local.observed = True
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(bundle, "_run", synchronize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bundle.prepare, root, destination=target) for root in (low, high)]
        failures = []
        for future in futures:
            try:
                assert future.result(timeout=10) == target
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Concurrent copy failed", failures)
    assert json.loads((target / "manifest.json").read_text())["version"] == "0.2.4"


def test_host_copy_uses_host_data_folder_and_only_existing_bridge(tmp_path, monkeypatch):
    from cassini_viewer import flatpak_bridge

    root = source(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "sandbox-private"))
    monkeypatch.setattr(bundle, "in_flatpak", lambda: True)
    monkeypatch.setattr(flatpak_bridge, "deployment", lambda: None)
    monkeypatch.setattr(
        flatpak_bridge, "host_info", lambda: (tmp_path / "config", tmp_path / "host-data", "flatpak", {})
    )
    commands = []

    def host(command, **kwargs):
        commands.append(command)
        return command

    monkeypatch.setattr(flatpak_bridge, "host_command", host)
    result = bundle.prepare(root)
    assert result == tmp_path / "host-data/cassini/browser-companion"
    assert (result / "manifest.json").exists() and not (tmp_path / "sandbox-private").exists()
    assert commands and all(command[:2] == ["/bin/sh", "-c"] for command in commands)


def test_copy_needs_no_host_python_or_diffutils(tmp_path, monkeypatch):
    root = source(tmp_path)
    commands = tmp_path / "coreutils"
    commands.mkdir()
    for name in ("head", "mkdir", "rmdir", "mv", "rm", "base64", "mktemp", "chmod", "sha256sum", "sleep"):
        (commands / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(commands))
    result = bundle.prepare(root, destination=tmp_path / "destination")
    assert (result / "manifest.json").exists()


def test_store_flag_defaults_off_and_accepts_only_json_boolean_true(tmp_path):
    config = tmp_path / "paths.json"
    assert not bundle.store_listing_public(config)
    for index, value in enumerate((False, True, "true", None)):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(dict(storeListingPublic=value)))
        assert bundle.store_listing_public(path) is (value is True)


def test_host_copy_timeout_is_an_os_error_and_cannot_break_an_existing_connection(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(bundle.subprocess, "run", timeout)
    with pytest.raises(OSError, match="timed out"):
        bundle.prepare(source(tmp_path), destination=tmp_path / "destination")


def test_an_owner_can_release_its_lock_after_mkdir_reports_busy(tmp_path, monkeypatch):
    root = source(tmp_path)
    target = tmp_path / "data/browser-companion"
    commands = tmp_path / "commands"
    commands.mkdir()
    marker = tmp_path / "released"
    lock = target.parent / ".browser-companion.lock"
    mkdir = shutil.which("mkdir")
    command = commands / "mkdir"
    command.write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = {shlex.quote(str(lock))} ] && [ ! -f {shlex.quote(str(marker))} ]; then\n'
        f"  : > {shlex.quote(str(marker))}\n"
        f'  {shlex.quote(mkdir)} -- "$2"\n'
        '  rmdir -- "$2"\n'
        "  exit 1\n"
        "fi\n"
        f'exec {shlex.quote(mkdir)} "$@"\n'
    )
    command.chmod(0o700)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.defpath)
    bundle.prepare(root, destination=target)
    assert marker.exists() and (target / "manifest.json").is_file()
    assert not lock.exists()
