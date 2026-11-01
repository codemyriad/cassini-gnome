# SPDX-License-Identifier: GPL-3.0-or-later
"""D50.3 calendar words and CLDR full-date patterns, with no locale mutation."""

import json
import re
from pathlib import Path

from .i18n import language

CALENDARS = json.loads(Path(__file__).with_name("cldr-dates.json").read_text())["locales"]


def month(date, *, abbreviated=False):
    key = "monthsAbbreviated" if abbreviated else "months"
    return CALENDARS[language()][key][date.month - 1]


def weekday(date):
    return CALENDARS[language()]["weekdays"][date.weekday()]


def full_date(date):
    calendar = CALENDARS[language()]
    values = {"EEEE": weekday(date), "MMMM": month(date), "d": str(date.day), "y": str(date.year)}

    def token(match):
        value = match.group()
        if value.startswith("'"):
            return value[1:-1].replace("''", "'")
        # Only the six pinned patterns are supported; future unsupported
        # fields must fail visibly rather than silently change a date.
        return values[value]

    return re.sub(r"'(?:''|[^'])*'|[A-Za-z]+", token, calendar["fullDate"])
