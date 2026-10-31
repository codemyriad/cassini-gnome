# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinator-owned English copy, preserved byte for byte in strings.json."""

import json
from pathlib import Path

CATALOG = {
    entry["id"]: entry["en"] for entry in json.loads(Path(__file__).with_name("strings.json").read_text())["entries"]
}


def text(identifier, **values):
    return CATALOG[identifier].format(**values)


PLURALS = {
    entry["id"]: entry["plural"]
    for entry in json.loads(Path(__file__).with_name("strings.json").read_text())["entries"]
    if "plural" in entry
}


def plural(identifier, n, **values):
    return PLURALS[identifier]["one" if n == 1 else "other"].format(n=n, **values)
