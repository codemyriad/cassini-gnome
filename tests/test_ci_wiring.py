# SPDX-License-Identifier: GPL-3.0-or-later
"""Every UI and recording script runs in CI: a scenario from a workflow,
a helper from the script or test that uses it."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(
    p.relative_to(ROOT).as_posix() for d in ("tests/ui", "tests/recording") for p in (ROOT / d).glob("*.py")
)
WORKFLOWS = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / ".github/workflows").glob("*.yml")))
USERS = {
    p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
    for d in ("tests", "build-aux")
    for p in (ROOT / d).rglob("*")
    if p.suffix in (".py", ".mjs", ".sh") and p.is_file() and not {"node_modules", "artifacts"} & set(p.parts)
}


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_ui_and_recording_script_runs_in_ci(script):
    if script in WORKFLOWS:
        return
    name, stem = Path(script).name, Path(script).stem
    users = [
        path
        for path, text in USERS.items()
        if path != script and (name in text or f"import {stem}\n" in text or f"from {stem} import" in text)
    ]
    assert users, f"{script} runs in no workflow and no other script uses it"
