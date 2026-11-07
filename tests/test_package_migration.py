# SPDX-License-Identifier: GPL-3.0-or-later
"""The package-manager smoke must never run on a user's ordinary installation."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("opt_in,container", [(False, False), (False, True), (True, False)])
def test_native_migration_requires_both_explicit_opt_in_and_container_marker(tmp_path, monkeypatch, opt_in, container):
    spec = importlib.util.spec_from_file_location("package_migration", ROOT / "build-aux/ci/package_migration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("CASSINI_TEST_PACKAGE_MIGRATION", "1" if opt_in else "0")
    original_exists = Path.exists

    def exists(path):
        if str(path) in ("/.dockerenv", "/run/.containerenv"):
            return container
        return original_exists(path)

    def unexpected(*_args, **_kwargs):
        pytest.fail("package database was consulted before the disposable-container guard")

    monkeypatch.setattr(module, "installed", unexpected)
    monkeypatch.setattr(module, "command", unexpected)
    with monkeypatch.context() as patch:
        patch.setattr(module.Path, "exists", exists)
        with pytest.raises(RuntimeError, match="disposable build container"):
            module.run("deb", tmp_path / "unused.deb")
