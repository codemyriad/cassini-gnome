# SPDX-License-Identifier: GPL-3.0-or-later
"""Only network preparation is retried; package failures must remain visible."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build-aux/ci/apt-install-bounded.sh"


def _write_mock_apt(binaries: Path) -> None:
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
        " if mode=='network-always':sys.exit(100)\n"
        " if mode=='network-once' and count==1:sys.exit(100)\n"
        " if mode=='network-twice' and count in (1,2):sys.exit(100)\n"
        "if '--no-download' in sys.argv and mode=='install-fails':sys.exit(42)\n"
    )
    apt.chmod(0o755)


@pytest.mark.parametrize("has_ca_certs", [True, False])
@pytest.mark.parametrize("layout", ["direct", "mirror-list"])
@pytest.mark.parametrize(
    "mode,success,downloads,installs",
    [
        ("ok", True, 1, 1),
        ("network-once", True, 2, 1),
        ("network-twice", True, 3, 1),
        ("network-always", False, 3, 0),
        ("install-fails", False, 1, 1),
    ],
)
def test_network_retry_is_bounded_and_never_repeats_package_configuration(
    tmp_path, mode, success, downloads, installs, layout, has_ca_certs
):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_mock_apt(binaries)

    sources = tmp_path / "apt/sources.list.d"
    sources.mkdir(parents=True)
    config = sources / "ubuntu.sources"
    if layout == "mirror-list":
        source = sources.parent / "apt-mirrors.txt"
        source.write_text("http://azure.archive.ubuntu.com/ubuntu/\nhttp://archive.ubuntu.com/ubuntu/\n")
        source.chmod(0o644)
        config.write_text(
            "URIs: mirror+file:" + str(source) + "\nSuites: resolute\n\n"
            "URIs: http://security.ubuntu.com/ubuntu/\nSuites: resolute-security\n"
        )
    else:
        source = config
        source.write_text(
            "URIs: http://azure.archive.ubuntu.com/ubuntu\nSuites: resolute\n\n"
            "URIs: http://security.ubuntu.com/ubuntu/\nSuites: resolute-security\n"
        )
    config.chmod(0o644)
    original = source.read_text()
    configured = config.read_text()
    third_party = sources / "other.list"
    third_party.write_text("deb https://example.invalid/other stable main\n")

    certs_file = tmp_path / "ca-certificates.crt"
    if has_ca_certs:
        certs_file.write_text("CERT\n")

    log = tmp_path / "calls.json"
    env = dict(
        os.environ,
        PATH=str(binaries) + ":" + os.environ["PATH"],
        APT_TEST_LOG=str(log),
        APT_TEST_MODE=mode,
        CASSINI_APT_SOURCES_DIR=str(sources.parent),
        CASSINI_SSL_CERTS_FILE=str(certs_file),
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--no-install-recommends", "python3-gi"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = json.loads(log.read_text())
    assert (result.returncode == 0) is success
    for call in calls:
        if "--no-download" not in call:
            assert "Acquire::Retries=1" in call
            assert "Acquire::http::Timeout=15" in call and "Acquire::https::Timeout=15" in call
            assert "APT::Update::Error-Mode=any" in call
        if "install" in call:
            assert "--no-install-recommends" in call
            assert "python3-gi" in call
    assert sum("--download-only" in row for row in calls) == downloads
    assert sum("--no-download" in row for row in calls) == installs
    assert third_party.read_text() == "deb https://example.invalid/other stable main\n"

    conf_file = sources.parent / "apt.conf.d/80cassini-network"
    assert conf_file.is_file()
    assert (conf_file.stat().st_mode & 0o444) == 0o444
    assert (source.stat().st_mode & 0o444) == 0o444
    assert (config.stat().st_mode & 0o444) == 0o444

    expected_scheme = "https" if has_ca_certs else "http"
    expected_kernel_url = f"{expected_scheme}://mirrors.edge.kernel.org/ubuntu/"

    if mode == "network-once":
        assert "network retry 2/3" in result.stderr
        assert "network retry 3/3" not in result.stderr
        if layout == "mirror-list":
            assert source.read_text() == "http://archive.ubuntu.com/ubuntu/\n"
            assert config.read_text() == configured
        else:
            assert source.read_text() == original.replace(
                "://azure.archive.ubuntu.com/ubuntu", "://archive.ubuntu.com/ubuntu"
            )
        assert str(source) in result.stdout
    elif mode in ("network-twice", "network-always"):
        assert "network retry 2/3" in result.stderr
        assert "network retry 3/3" in result.stderr
        if layout == "mirror-list":
            assert source.read_text() == expected_kernel_url + "\n"
            assert expected_kernel_url in config.read_text()
            assert "security.ubuntu.com" not in config.read_text()
        else:
            assert source.read_text().count(expected_kernel_url) == 2
            assert "ubuntu.com/ubuntu" not in source.read_text()
        assert str(source) in result.stdout
    else:
        assert "network retry" not in result.stderr
    if mode == "install-fails":
        assert result.returncode == 42


def test_minimal_container_without_python3_rewrites_mirrors_and_rejects_missing_packages(tmp_path):
    """meson-tests.yml and build.yml run in bare ubuntu:26.04 before python3 is installed."""
    binaries = tmp_path / "minimal-bin"
    binaries.mkdir()
    _write_mock_apt(binaries)
    for tool in ("bash", "timeout", "sed", "grep", "cmp", "cat", "cp", "rm", "mkdir", "chmod", "mktemp", "basename"):
        found = shutil.which(tool)
        assert found is not None, tool
        (binaries / tool).symlink_to(found)

    sources = tmp_path / "apt/sources.list.d"
    sources.mkdir(parents=True)
    ubuntu_sources = sources / "ubuntu.sources"
    ubuntu_sources.write_text(
        "URIs: http://archive.ubuntu.com/ubuntu/\nSuites: resolute\n\n"
        "URIs: http://security.ubuntu.com/ubuntu/\nSuites: resolute-security\n"
    )

    # Reject invocations without at least one package name (even when flags are passed).
    for bad_args in ([], ["--no-install-recommends"]):
        bad = subprocess.run(
            [str(binaries / "bash"), str(SCRIPT), *bad_args],
            env={"PATH": str(binaries)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert bad.returncode == 2

    log = tmp_path / "calls.json"
    res = subprocess.run(
        [str(binaries / "bash"), str(SCRIPT), "--no-install-recommends", "meson", "python3"],
        env={
            "PATH": str(binaries),
            "APT_TEST_LOG": str(log),
            "APT_TEST_MODE": "network-twice",
            "CASSINI_APT_SOURCES_DIR": str(sources.parent),
            "CASSINI_SSL_CERTS_FILE": str(tmp_path / "missing-ca-certs.crt"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert ubuntu_sources.read_text().count("http://mirrors.edge.kernel.org/ubuntu/") == 2
