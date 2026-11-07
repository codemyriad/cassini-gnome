# SPDX-License-Identifier: GPL-3.0-or-later
"""Only network preparation is retried; package failures must remain visible."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "mode,success,downloads,installs",
    [("ok", True, 1, 1), ("network-once", True, 2, 1), ("network-always", False, 2, 0), ("install-fails", False, 1, 1)],
)
def test_network_retry_is_bounded_and_never_repeats_package_configuration(tmp_path, mode, success, downloads, installs):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    apt = binaries / "apt-get"
    apt.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "log=Path(os.environ['APT_TEST_LOG'])\n"
        "rows=json.loads(log.read_text()) if log.exists() else []\n"
        "rows.append(sys.argv[1:]);log.write_text(json.dumps(rows))\n"
        "mode=os.environ['APT_TEST_MODE']\n"
        "if '--download-only' in sys.argv:\n"
        " count=sum('--download-only' in r for r in rows)\n"
        " if mode=='network-always' or mode=='network-once' and count==1:sys.exit(100)\n"
        "if '--no-download' in sys.argv and mode=='install-fails':sys.exit(42)\n"
    )
    apt.chmod(0o755)
    sources = tmp_path / "apt/sources.list.d"
    sources.mkdir(parents=True)
    source = sources / "ubuntu.sources"
    source.write_text("URIs: http://azure.archive.ubuntu.com/ubuntu\nSuites: resolute\n")
    third_party = sources / "other.list"
    third_party.write_text("deb https://example.invalid/other stable main\n")
    log = tmp_path / "calls.json"
    env = dict(
        os.environ,
        PATH=str(binaries) + ":" + os.environ["PATH"],
        APT_TEST_LOG=str(log),
        APT_TEST_MODE=mode,
        CASSINI_APT_SOURCES_DIR=str(sources.parent),
    )
    result = subprocess.run(
        ["bash", str(ROOT / "build-aux/ci/apt-install-bounded.sh"), "python3-gi"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = json.loads(log.read_text())
    assert (result.returncode == 0) is success
    assert sum("--download-only" in row for row in calls) == downloads
    assert sum("--no-download" in row for row in calls) == installs
    assert third_party.read_text() == "deb https://example.invalid/other stable main\n"
    if mode.startswith("network"):
        assert "network retry 2/2" in result.stderr
        assert source.read_text() == "URIs: http://archive.ubuntu.com/ubuntu\nSuites: resolute\n"
    else:
        assert "network retry" not in result.stderr
    if mode == "install-fails":
        assert result.returncode == 42
