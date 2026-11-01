# SPDX-License-Identifier: GPL-3.0-or-later
"""UI language selection, independent of LC_TIME and LC_NUMERIC (D50)."""

import gettext as _gettext
import os
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

LANGUAGES = ("en", "it", "fr", "es", "pt-BR", "pt-PT")
_override = ContextVar("cassini_ui_language", default=None)
_localedir = None


def select_language(names):
    for name in names:
        parts = name.split(".", 1)[0].split("@", 1)[0].replace("_", "-").lower().split("-")
        base = parts[0]
        if base == "pt":
            region = next((part for part in parts[1:] if len(part) == 2 or (len(part) == 3 and part.isdigit())), None)
            return "pt-BR" if region in (None, "br") else "pt-PT"
        if base in LANGUAGES:
            return base
    return "en"


@lru_cache(maxsize=1)
def _system_language():
    try:
        from gi.repository import GLib
    except ImportError:
        # The stdlib-only native-host conformance entry point has no GTK.
        names = next((os.environ[k] for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(k)), "C")
        return select_language(names.split(":"))

    return select_language(GLib.get_language_names())


def language():
    return _override.get() or _system_language()


@contextmanager
def using_language(value):
    """Scope a document/test language without changing process formatting locales."""
    if value not in LANGUAGES:
        raise ValueError("Unsupported UI language")
    token = _override.set(value)
    try:
        yield
    finally:
        _override.reset(token)


def configure(localedir):
    global _localedir
    _localedir = localedir
    # GNU gettext and GTK use LANGUAGE; this never changes LC_TIME/NUMERIC.
    os.environ["LANGUAGE"] = language().replace("-", "_")
    _gettext.bindtextdomain("cassini-talk", localedir)
    _gettext.textdomain("cassini-talk")


@lru_cache(maxsize=12)
def _messages(lang, directory):
    return _gettext.translation("cassini-talk", localedir=directory, languages=[lang.replace("-", "_")], fallback=True)


def gettext(message):
    return _messages(language(), _localedir or str(Path(__file__).parents[2] / "locale")).gettext(message)


def ngettext(singular, multiple, count):
    return _messages(language(), _localedir or str(Path(__file__).parents[2] / "locale")).ngettext(
        singular, multiple, count
    )


def plural_category(value, *, lang=None):
    """CLDR cardinal rules for ordinary numbers in our six UI languages.

    CLDR 49: https://unicode.org/cldr/charts/49/supplemental/language_plural_rules.html
    No compact-number exponent is used by Cassini's counts. Visible decimal
    places are retained for Decimal inputs (1 and 1.0 can select differently).
    """
    lang = language() if lang is None else lang
    number = abs(Decimal(str(value)))
    if not number.is_finite():
        raise ValueError("A plural count must be finite")
    integer = int(number)
    decimals = max(0, -number.as_tuple().exponent)
    if lang in ("fr", "pt-BR"):
        one = integer in (0, 1)
    elif lang == "es":
        one = number == 1
    else:
        one = integer == 1 and decimals == 0
    if one:
        return "one"
    if lang != "en" and integer != 0 and integer % 1_000_000 == 0 and decimals == 0:
        return "many"
    return "other"
