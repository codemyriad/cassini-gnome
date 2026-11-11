# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 zero suppression, localized summaries and error categories."""

import pytest

from cassini_talk.batch_import import Result
from cassini_talk.i18n import using_language
from cassini_talk.import_feedback import progress, summary
from cassini_talk.strings import text


@pytest.mark.parametrize("locale", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_import_summary_uses_all_published_languages_and_omits_zero_parts(locale):
    with using_language(locale):
        value = summary(Result(imported=1, duplicates=2))
        assert len(value.split(" · ")) == 2
        assert "1" in value and "2" in value and "0" not in value
        if locale != "en":
            assert value != "Imported 1 · Skipped 2 duplicates"
        assert progress(Result(total=4, imported=1)) == text("import.progress", done=2, total=4)


def test_cancelled_summary_keeps_success_and_reports_cancellation():
    with using_language("en"):
        assert summary(Result(imported=1, cancelled=True)) == "Imported 1 · Import cancelled"
        assert summary(Result()) == text("import.zip.failed.empty")
        value = summary(Result(failed=1, errors=[("Unsafe.zip", "unsafe")]))
        assert text("import.zip.failed.unsafePaths") in value
