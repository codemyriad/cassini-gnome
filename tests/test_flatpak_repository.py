# SPDX-License-Identifier: GPL-3.0-or-later
"""A future-user release repository signs only the current identity and locales."""

import base64
import importlib.util
from pathlib import Path

import pytest


def helper():
    path = Path(__file__).parents[1] / "build-aux/flatpak/prepare-release.py"
    spec = importlib.util.spec_from_file_location("prepare_flatpak_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("unexpected", [False, True])
def test_release_signs_current_refs_and_never_rebases_or_accepts_another_app(tmp_path, monkeypatch, unexpected):
    module = helper()
    repo, output, keys = [tmp_path / name for name in ("repo", "out", "keys")]
    repo.mkdir()
    keys.mkdir()
    calls = []
    public = b"synthetic public key for command orchestration only"

    def call(command, *, timeout):
        assert timeout == 300
        calls.append(command)
        if command[0] == "gpg":
            assert "--export" in command and "--export-secret-keys" not in command
            return public
        if command[1:3] == ("repo", "--branches"):
            refs = [f"app/{module.APP}/x86_64/master", f"runtime/{module.APP}.Locale/x86_64/master"]
            if unexpected:
                refs.append("app/org.example.Unrelated/x86_64/master")
            return ("\n".join(refs) + "\n").encode()
        if command[1] == "--default-arch":
            return b"x86_64\n"
        if command[1] == "build-bundle":
            Path(command[-2]).write_bytes(b"synthetic bundle")
        return b""

    monkeypatch.setattr(module.subprocess, "check_output", call)
    if unexpected:
        with pytest.raises(ValueError, match="unexpected ref"):
            module.prepare(repo, output, "a" * 40, keys)
        assert not output.exists()
        assert not any(command[1] == "build-sign" for command in calls)
        return
    module.prepare(repo, output, "a" * 40, keys)
    assert (output / "cassini-talk.flatpak").read_bytes() == b"synthetic bundle"
    assert (output / "cassini-talk.gpg").read_bytes() == public
    description = (output / "cassini-talk.flatpakrepo").read_text()
    assert f"GPGKey={base64.b64encode(public).decode()}" in description and f"Url={module.URL}" in description
    signed = [command[-2] for command in calls if command[1] == "build-sign"]
    assert signed == [module.APP, module.APP + ".Locale"]
    assert any(command[1] == "build-update-repo" for command in calls)
    assert all(command[0] != "sh" and command[1] != "build-commit-from" for command in calls)
