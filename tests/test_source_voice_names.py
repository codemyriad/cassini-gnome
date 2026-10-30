# SPDX-License-Identifier: GPL-3.0-or-later
"""D35 generic source/display labels cannot enrol a remembered person."""

import pytest

from cassini_viewer.voices_logic import person_name


@pytest.mark.parametrize(
    "value", ["", " \t ", "Me", "The others", "  the OTHERS  ", "Speaker 1", "Speaker 103", "speaker ٢"]
)
def test_source_labels_and_unnamed_numbers_are_not_people(value):
    assert not person_name(value, ("Me", "The others"))


@pytest.mark.parametrize("value", ["Marta", "A speaker", "Speaker Jones", "Élodie", "李华", "Other people"])
def test_entered_names_can_be_remembered(value):
    assert person_name(value, ("Me", "The others"))


def test_source_labels_follow_the_display_catalog():
    assert not person_name("  GLI ALTRI ", ("Io", "Gli altri"))
    assert person_name("The others", ("Io", "Gli altri"))
