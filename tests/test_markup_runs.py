# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
from xml.etree import ElementTree

import pytest

from cassini_talk.markup_runs import bridge_link_spaces


def test_bridging_preserves_all_text_word_links_and_highlight_boundaries():
    markup = (
        '<a href="item:0"><span background="#BDDCE4" foreground="#191B1F">Now</span></a> '
        '<a href="item:1"><span alpha="60%">later</span></a> '
        '<a href="aside:0:0">aside &amp; text</a>'
    )
    result = bridge_link_spaces(markup, (0, 0, 6 / 255, 0.8))
    before, after = [ElementTree.fromstring("<root>" + value + "</root>") for value in (markup, result)]
    assert "".join(before.itertext()) == "".join(after.itertext())
    for a, b in zip(before.iter("a"), after.iter("a"), strict=True):
        a.tail = b.tail = None
        assert ElementTree.tostring(a) == ElementTree.tostring(b)
    assert 'alpha="60%"> </span>' in result
    assert 'alpha="52428"> </span>' in result
    assert 'foreground="#000000000606"' in result


def test_only_generated_adjacent_seek_links_are_bridged():
    markup = '<a href="item:0">a</a> (name: <a href="aside:0:0">b</a>) <a href="https://example.invalid">c</a>'
    assert bridge_link_spaces(markup, (1, 1, 1, 1)) == markup


def test_nested_chip_background_and_separators_remain_exactly_as_stored():
    markup = (
        '<span background="#191B1F" bgalpha="10%"> <b>Sam:</b> '
        '<a href="aside:0:0"><span alpha="60%">Sounds</span></a> '
        '<a href="aside:0:1"><span alpha="60%">good.</span></a> </span>'
    )
    assert bridge_link_spaces(markup, (0, 0, 6 / 255, 0.8)) == markup


@pytest.mark.parametrize("colour,rgb", [((1, 1, 1, 1), "#ffffffffffff"), ((0, 0, 0, 1), "#000000000000")])
def test_opaque_dark_and_high_contrast_separators_omit_alpha(colour, rgb):
    markup = '<a href="item:0">before</a> <a href="item:1">after</a>'
    assert bridge_link_spaces(markup, colour) == (
        f'<a href="item:0">before</a><span foreground="{rgb}"> </span><a href="item:1">after</a>'
    )


@pytest.mark.parametrize("opacity,expected", [(1.2, ""), (-0.2, ' alpha="1"'), (0, ' alpha="1"')])
def test_rgba_channels_are_clamped_to_valid_pango_values(opacity, expected):
    markup = '<a href="item:0">before</a> <a href="item:1">after</a>'
    assert bridge_link_spaces(markup, (-0.2, 0.5, 1.2, opacity)) == (
        f'<a href="item:0">before</a><span foreground="#00008000ffff"{expected}> </span><a href="item:1">after</a>'
    )


def test_host_runs_resume_after_a_shaded_chip_closes():
    chip = '<span background="#000006"><a href="aside:0:0">Sounds</a> <a href="aside:0:1">good.</a></span>'
    markup = chip + ' <a href="item:0">back</a> <a href="item:1">again</a>'
    assert bridge_link_spaces(markup, (1, 1, 1, 1)) == (
        chip + ' <a href="item:0">back</a><span foreground="#ffffffffffff"> </span><a href="item:1">again</a>'
    )


def test_stored_markup_literals_remain_text_with_their_original_seek_targets():
    from cassini_talk.meeting_text import reading_markup, reading_turns
    from cassini_talk.portable import Word

    values = ['<a href="item:0">', "</a>", '<span background="#000">', 'alpha="60%"']
    turn = reading_turns([Word("a", i * 100, i * 100 + 90, value) for i, value in enumerate(values)])[0]
    markup = reading_markup(turn, 1000, lambda _: "Ada")
    result = bridge_link_spaces(markup, (1, 1, 1, 1))
    before, after = [ElementTree.fromstring("<root>" + value + "</root>") for value in (markup, result)]
    assert "".join(before.itertext()) == "".join(after.itertext())
    assert [a.attrib for a in before.iter("a")] == [a.attrib for a in after.iter("a")]
    assert 'alpha="60%"' not in result
