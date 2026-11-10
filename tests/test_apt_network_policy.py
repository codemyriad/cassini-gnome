# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every CI apt entry point bounds acquisition and rejects partial indexes."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCKERFILES = (
    "tests/whistle/Containerfile",
    "tests/desktop/Dockerfile",
    "tests/build-refresh/Dockerfile",
    "tests/browser-setup/Dockerfile",
    "tests/recording/Audio.Dockerfile",
    "build-aux/deb/Dockerfile",
)
POLICY = {
    "Acquire::Retries": "5",
    "Acquire::Retries::Delay": "true",
    "Acquire::Retries::Delay::Maximum": "5",
    "Acquire::http::Timeout": "15",
    "Acquire::https::Timeout": "15",
    "APT::Update::Error-Mode": "any",
}


@pytest.mark.parametrize("name", DOCKERFILES)
def test_container_apt_policy_precedes_all_network_calls_and_survives_refresh(name):
    text = (ROOT / name).read_text()
    first_call = re.search(r"\bapt-get\s+(?:update|install)", text).start()
    before = text[:first_call]
    settings = dict(re.findall(r"'([\w:-]+) \"([^\"]+)\";'", before))
    assert all(settings.get(key) == value for key, value in POLICY.items())
    assert "/etc/apt/apt.conf.d/80cassini-network" in before
    assert text.index("FROM ") < before.index('Acquire::Retries "5";')
    if "FROM ${HARNESS}" in text:
        # The cacheable harness owns the file, so the refreshed stage inherits
        # it. Its Dockerfile participates in the existing harness cache key.
        assert before.index('Acquire::Retries "5";') < text.index("FROM ${HARNESS}")
        assert "RUN apt-get update && apt-get -y --no-install-recommends dist-upgrade" in text


def test_workflow_apt_commands_have_both_timeouts_retries_and_strict_indexes():
    seen = 0
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for line in path.read_text().splitlines():
            if re.search(r"\bapt-get\s", line):
                seen += 1
                for key, value in POLICY.items():
                    assert f"-o {key}={value}" in line, (path.name, line, key)
    assert seen > 0
