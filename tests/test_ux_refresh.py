# SPDX-License-Identifier: GPL-3.0-or-later
"""The explicit catalog refresh preserves approved bytes and complete snapshots."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp_path):
    project = tmp_path / "project"
    script = project / "build-aux/check-ux-vectors.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "build-aux/check-ux-vectors.py", script)
    target = project / "tests/ux"
    target.mkdir(parents=True)
    provenance = dict(commit="old", sha256={name: hashlib.sha256(b"old").hexdigest() for name in ("a", "b")})
    (target / "UPSTREAM.json").write_text(json.dumps(provenance))
    for name in ("a", "b"):
        (target / name).write_bytes(b"old")
    source = tmp_path / "catalog"
    source.mkdir()
    for name, data in (("a", b"approved\r\n"), ("b", "café\n".encode())):
        (source / name).write_bytes(data)
    return script, target, source


def test_explicit_refresh_copies_exact_catalog_bytes_and_updates_the_golden(tmp_path):
    script, target, source = fixture(tmp_path)
    result = subprocess.run([sys.executable, str(script), "--source", str(source)], capture_output=True)
    assert result.returncode != 0
    subprocess.run(
        [sys.executable, str(script), "--refresh", "--source", str(source), "--commit", "approved-revision"],
        check=True,
        capture_output=True,
    )
    assert (target / "a").read_bytes() == b"approved\r\n"
    assert (target / "b").read_bytes() == "café\n".encode()
    assert json.loads((target / "UPSTREAM.json").read_text())["commit"] == "approved-revision"
    subprocess.run([sys.executable, str(script), "--source", str(source)], check=True, capture_output=True)


def test_incomplete_refresh_does_not_modify_any_existing_golden(tmp_path):
    script, target, source = fixture(tmp_path)
    (source / "b").unlink()
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    result = subprocess.run(
        [sys.executable, str(script), "--refresh", "--source", str(source), "--commit", "approved-revision"],
        capture_output=True,
    )
    assert result.returncode != 0
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_refresh_requires_an_explicit_source_and_revision(tmp_path):
    script, target, _source = fixture(tmp_path)
    before = (target / "UPSTREAM.json").read_bytes()
    result = subprocess.run([sys.executable, str(script), "--refresh"], capture_output=True)
    assert result.returncode == 2
    assert b"requires --source and --commit" in result.stderr
    assert (target / "UPSTREAM.json").read_bytes() == before


def test_refresh_and_check_resolve_source_paths_mapping(tmp_path):
    script, target, _ = fixture(tmp_path)
    upstream_repo = tmp_path / "cassini-ux"
    (upstream_repo / "contract").mkdir(parents=True)
    (upstream_repo / "vectors/states").mkdir(parents=True)
    (upstream_repo / "contract/STATUS-BADGES.md").write_bytes(b"# Status badges\n")
    (upstream_repo / "vectors/states/states-vectors.json").write_bytes(b'{"version": 3}\n')
    provenance = {
        "source": "https://github.com/codemyriad/cassini-ux",
        "commit": "old",
        "paths": {
            "STATUS-BADGES.md": "tests/ux/STATUS-BADGES.md",
            "states-vectors.json": "tests/ux/states-vectors.json",
        },
        "sha256": {
            "STATUS-BADGES.md": "",
            "states-vectors.json": "",
        },
        "sourcePaths": {
            "STATUS-BADGES.md": "contract/STATUS-BADGES.md",
            "states-vectors.json": "vectors/states/states-vectors.json",
        },
    }
    (target / "UPSTREAM.json").write_text(json.dumps(provenance))
    subprocess.run(
        [sys.executable, str(script), "--refresh", "--source", str(upstream_repo), "--commit", "793209cb"],
        check=True,
        capture_output=True,
    )
    assert (target / "STATUS-BADGES.md").read_bytes() == b"# Status badges\n"
    assert (target / "states-vectors.json").read_bytes() == b'{"version": 3}\n'
    subprocess.run([sys.executable, str(script), "--source", str(upstream_repo)], check=True, capture_output=True)
