# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact wrapping and cache invalidation for recycled transcript labels."""

from unittest.mock import patch

from gi.repository import Gtk, Pango

from cassini_talk import transcript_label
from cassini_talk.transcript_label import TranscriptLabel


def check_measurements(window):
    popup = Gtk.Popover()
    popup.set_parent(window)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["meeting-transcript"])
    popup.set_child(box)
    markup = " ".join(f'<a href="word:{i}">Synthetic</a>' for i in range(90))
    widgets = []

    def label(kind, value=markup, **kwargs):
        widget = kind(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, use_markup=True, css_classes=["words"], **kwargs)
        widget.set_markup(value)
        box.append(widget)
        widgets.append(widget)
        return widget

    def compare(reference, candidate):
        for width in (100, 240, 480, 800):
            expected = reference.measure(Gtk.Orientation.VERTICAL, width)
            actual = candidate.measure(Gtk.Orientation.VERTICAL, width)
            assert tuple(actual) == tuple(expected), (width, actual, expected)

    try:
        reference = label(Gtk.Label)
        candidate = label(TranscriptLabel)
        compare(reference, candidate)
        # The exact container owns the text-column width. Neither minimum
        # nor unwrapped natural width may shape an unused paragraph layout;
        # every real height-for-width remains identical to a native label.
        horizontal = candidate.measure(Gtk.Orientation.HORIZONTAL, -1)
        assert horizontal.minimum == horizontal.natural, tuple(horizontal)
        assert horizontal.natural < reference.measure(Gtk.Orientation.HORIZONTAL, -1).natural
        calls = []
        original = TranscriptLabel._measure_uncached

        def uncached(widget, orientation, for_size):
            calls.append((orientation, for_size))
            return original(widget, orientation, for_size)

        # Warm native font discovery before counting cache reuse across widgets.
        compare(reference, label(TranscriptLabel))
        with patch.object(TranscriptLabel, "_measure_uncached", uncached):
            clone = label(TranscriptLabel)
            compare(reference, clone)
            assert not calls, "Recycling an identical label repeated native text measurement"
            # Link destinations are navigation identity, not text geometry.
            clone2 = label(TranscriptLabel, markup.replace("word:", "other:"))
            compare(reference, clone2)
            assert not calls, "Changing only a link destination repeated text measurement"
            # Each geometry change must miss the shared cache and match GTK.
            for changed in (markup + " Additional words.", '<span size="18000">' + markup + "</span>"):
                before = len(calls)
                compare(label(Gtk.Label, changed), label(TranscriptLabel, changed))
                assert len(calls) > before
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_line_height_new(2.0))
            a, b = label(Gtk.Label), label(TranscriptLabel)
            a.set_attributes(attrs)
            b.set_attributes(attrs)
            before = len(calls)
            compare(a, b)
            assert len(calls) > before, "Changed line-height reused stale measurements"
        # CSS metrics live in the resolved layout attributes, not just the
        # label's explicit Pango attributes or default font description.
        provider = Gtk.CssProvider()
        provider.load_from_string(".measure-font-large { font-size: 23px; line-height: 1.7; letter-spacing: 0.7px; }")
        Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        try:
            a, b = label(Gtk.Label), label(TranscriptLabel)
            a.add_css_class("measure-font-large")
            b.add_css_class("measure-font-large")
            compare(a, b)
            assert tuple(b.measure(Gtk.Orientation.VERTICAL, 240)) != tuple(
                candidate.measure(Gtk.Orientation.VERTICAL, 240)
            )
        finally:
            Gtk.StyleContext.remove_provider_for_display(window.get_display(), provider)
        # Cache storage stays bounded even across many different recordings.
        with patch.object(transcript_label, "_LIMIT", 8):
            transcript_label._MEASUREMENTS.clear()
            for index in range(20):
                label(TranscriptLabel, f"Distinct synthetic note {index}.").measure(Gtk.Orientation.VERTICAL, 240)
            assert len(transcript_label._MEASUREMENTS) <= 8
    finally:
        transcript_label._MEASUREMENTS.clear()
        popup.unparent()
    print("PASS exact transcript wrapping, cross-widget reuse and geometry invalidation", flush=True)
