# SPDX-License-Identifier: GPL-3.0-or-later
"""G8: the flatpak-builder cache is keyed on its manifest, never per commit."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"


def builder_steps():
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        steps = re.split(r"\n\s+- (?=uses:|name:)", workflow.read_text())
        for step in steps:
            if "flatpak-github-actions/flatpak-builder@" in step:
                yield workflow.name, step


def test_every_builder_cache_key_hashes_its_own_manifest():
    keys = {}
    for name, step in builder_steps():
        manifest = re.search(r"manifest-path: (\S+)", step).group(1)
        key = re.search(r"cache-key: (.+)", step).group(1).strip()
        assert "github.sha" not in key, (name, key)  # A per-commit key never hits.
        assert key.endswith("${{ hashFiles('" + manifest + "') }}"), (name, key)
        assert re.search(r"^\s+cache: ", step, re.M) is None, (name, "cache: false also stops the restore")
        keys.setdefault(key, set()).add(manifest)
    # The devel profile. The stable one is built by flatpak-builder itself in
    # flatpak.yml, with no action cache, as our closest test of the Flatpak
    # Flathub will build (D45.3).
    assert len(keys) == 1, keys
    prefixes = {key.split("${{")[0] for key in keys}
    assert len(prefixes) == len(keys), keys  # One manifest's entry never shadows another's.
