# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared GStreamer capture conversion, queue admission, and source EOS helpers."""

import threading

from gi.repository import GLib, Gst

# D56: a track is silent only when every PCM16 sample is 0. audioconvert's default
# TPDF dither turns digital silence from a float source into +-1 noise (a quarter
# of the samples), so every capture tap converts without dither, as export does.
CAPTURE_CONVERT = "audioconvert dithering=0 noise-shaping=0"

# R93-2 D69: NativeQueue admits ten seconds of converted PCM ahead of
# the writer. Its sink probe drops excess callback buffers and counts their
# exact frames. The C queue itself must not block before that admission rule.
TAP_QUEUE = "queue name=tap-queue max-size-time=0 max-size-buffers=0 max-size-bytes=0"


def request_eos(pipeline, rejected):
    """End each independent source without taking stream locks on GTK.

    Serial EOS delivery through a bin can wait on another interleave input.
    Give each source its own sender so all inputs can finish independently.
    The bus remains the authority for completed output; the existing watchdog
    handles sources that do not respond. A late rejection belongs to this
    pipeline only, never a subsequent recording.
    """
    iterator = pipeline.iterate_sources()
    sources = []
    while True:
        result, source = iterator.next()
        if result == Gst.IteratorResult.OK:
            sources.append(source)
        elif result == Gst.IteratorResult.RESYNC:
            iterator.resync()
            sources.clear()
        else:
            break

    def send(source):
        try:
            handled = source.send_event(Gst.Event.new_eos())
        except Exception:
            handled = False
        if not handled:
            GLib.idle_add(rejected, pipeline)

    for source in sources or [pipeline]:
        threading.Thread(target=send, args=(source,), name="cassini-capture-eos", daemon=True).start()
