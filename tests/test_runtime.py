# SPDX-License-Identifier: GPL-3.0-or-later

from cassini_viewer import runtime


def test_too_old():
    assert runtime.too_old("GTK", (4, 18, 0), (4, 18)) is None
    assert runtime.too_old("GTK", (4, 20, 1), (4, 18)) is None
    assert runtime.too_old("GTK", (5, 0, 0), (4, 18)) is None
    assert runtime.too_old("GTK", (4, 16, 7), (4, 18)) == "GTK 4.18 or newer is needed, but 4.16.7 is installed"
    assert runtime.too_old("libadwaita", (1, 7, 2), (1, 8)) is not None


def test_message_names_every_problem():
    text = runtime.message(["first problem", "second problem"])
    assert text.startswith("Cassini cannot start: first problem; second problem.")
    assert "\n" not in text


def test_missing_gst_elements():
    available = {"oggdemux": object()}
    assert runtime.missing_gst_elements(available.get) == ["opusdec"]
    assert runtime.missing_gst_elements(lambda _name: object()) == []
    assert "opusdec" in runtime.gst_warning(["opusdec"])


def test_check_passes_or_exits_with_message(capsys):
    # Runs against whatever the test machine has; either outcome must be clean.
    try:
        runtime.check()
    except SystemExit as exc:
        assert exc.code == 1
        assert capsys.readouterr().err.startswith("Cassini cannot start: ")
