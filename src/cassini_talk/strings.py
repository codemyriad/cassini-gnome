# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinator-owned copy, localized with the D50 catalog and CLDR rules."""

import json
from pathlib import Path

from .i18n import language, number, plural_category

ENTRIES = {
    entry["id"]: entry
    for entry in json.loads(Path(__file__).with_name("strings.json").read_text(encoding="utf-8"))["entries"]
}
CATALOG = {identifier: entry["en"] for identifier, entry in ENTRIES.items()}


def text(identifier, **values):
    entry = ENTRIES[identifier]
    localized = entry.get("l10n", {}).get(language(), {})
    return localized.get("text", entry["en"]).format(**{key: number(value) for key, value in values.items()})


PLURALS = {identifier: entry["plural"] for identifier, entry in ENTRIES.items() if "plural" in entry}


def plural(identifier, n, **values):
    entry = ENTRIES[identifier]
    forms = entry.get("l10n", {}).get(language(), {}).get("plural", entry["plural"])
    return forms.get(plural_category(n), forms["other"]).format(
        n=number(n), **{key: number(value) for key, value in values.items()}
    )
