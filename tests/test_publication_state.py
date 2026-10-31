# SPDX-License-Identifier: GPL-3.0-or-later
"""D48 publication facts stay distinct from capture/transcript errors."""

import errno
from dataclasses import replace
from pathlib import Path

from cassini_talk.library import Meeting
from cassini_talk.publication_state import failure_notice, project
from cassini_talk.strings import text


def meeting(**values):
    return Meeting(
        id="id",
        directory=Path("/Library/.cassini/sessions/Recording-id"),
        title="Test",
        started_at_ms=None,
        duration_ms=None,
        **values,
    )


def test_generic_and_disk_failures_use_approved_copy_without_an_interrupted_badge():
    for error, notice in (
        (OSError("encode"), "library.publishFailed"),
        (OSError(errno.ENOSPC, "full"), "library.publishDiskLow"),
    ):
        row = project(meeting(), error)
        assert row.publication_notice == notice and row.error is None
        assert text(notice) in row.row_texts()
        assert row.interrupted_at_ms is None


def test_source_warning_means_entire_source_empty_not_a_single_empty_span():
    row = meeting(
        published_file=Path("/Library/meeting.opus"),
        extensions={"codemyriad.io": {"tracks": [{"spans": [{"frames": 0}, {"frames": 480}]}]}},
    )
    assert project(row).publication_notice is None
    row = replace(
        row, extensions={"codemyriad.io": {"tracks": [{"spans": [{"frames": 480}]}, {"spans": [{"frames": 0}]}]}}
    )
    assert project(row).publication_notice == "library.publishSourcesEmpty"


def test_missing_file_wins_over_an_old_failure_and_never_offers_a_retry():
    row = project(meeting(published_missing=True), OSError("old encode failure"))
    assert row.publication_notice == "library.publishedMissing"
    assert failure_notice(OSError("other")) == "library.publishFailed"


def test_foreign_or_future_track_metadata_cannot_break_the_library_projection():
    for tracks in (None, 1, "future", [None], [{"spans": 2}], [{"spans": [None]}]):
        row = meeting(published_file=Path("/Library/file.opus"), extensions={"codemyriad.io": {"tracks": tracks}})
        assert project(row).publication_notice is None


def test_display_projection_never_queries_a_disconnected_library(monkeypatch):
    from cassini_talk.publication_state import session_root_path

    def blocked(*_args, **_kwargs):
        raise AssertionError("GTK tried to query a potentially disconnected Library")

    monkeypatch.setattr(Path, "stat", blocked)
    monkeypatch.setattr(Path, "lstat", blocked)
    directory = Path("/offline/Library/.cassini/sessions/Recording-12345678-1234-1234-1234-123456789abc")
    row = replace(meeting(capture_state="completed"), directory=directory)
    assert project(row).publication_notice is None
    assert session_root_path(directory) == Path("/offline/Library")
