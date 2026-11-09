# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
from xml.etree import ElementTree

from cassini_talk.markup_runs import bridge_link_spaces


def test_bridging_preserves_all_text_word_links_and_highlight_boundaries():
    markup = (
        '<a href="item:0"><span background="#fbe7a1" foreground="#202024">Now</span></a> '
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
        '<span background="#000006" bgalpha="7%"> <b>Sam:</b> '
        '<a href="aside:0:0"><span alpha="60%">Sounds</span></a> '
        '<a href="aside:0:1"><span alpha="60%">good.</span></a> </span>'
    )
    assert bridge_link_spaces(markup, (0, 0, 6 / 255, 0.8)) == markup
