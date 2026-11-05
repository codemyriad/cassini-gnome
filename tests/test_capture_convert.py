"""D56: every capture tap keeps digital silence all zeros.

silent_tracks counts a 32 ms frame as sound when any PCM16 sample is != 0, so a
silent source must reach the file as exact zeros. pulsesrc may hand float PCM to
the tap's audioconvert, whose default TPDF dither would turn that silence into
+-1 noise. Each tap's own description (pulsesrc swapped for an appsrc) is run
on float silence and on a -144 dBFS constant (D68.1's keep-real-time level, as a
browser's output reaches the monitor), and must give only zeros.
"""

import array
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cassini_talk import native_recorder, native_tap, recorder  # noqa: E402

Gst.init(None)


class Described(Exception):
    pass


def descriptions(monkeypatch):
    """The real (pulsesrc) description of each capture tap, as it is parsed."""
    seen = []

    def record(description, *_):
        seen.append(description)
        raise Described

    monkeypatch.setattr(Gst, "parse_launch", record)
    monkeypatch.setattr(Gst, "parse_bin_from_description", record)
    for kind in ("microphone", "system"):
        with pytest.raises(Described):
            native_tap.NativeTap(kind, 0, lambda *_: None, lambda *_: None).start()
        with pytest.raises(Described):
            native_recorder.NativeRecorder._branch(SimpleNamespace(_test_sources=False), kind)
    monkeypatch.undo()
    taps = [d for d in recorder.capture_pipeline().split(" capture.sink_") if "pulsesrc" in d]
    return seen + [d[d.index("pulsesrc") :] for d in taps]


def converted(chain, level, rate):
    """Non-zero S16 samples and their total, for 2 s of float stereo at `level`."""
    pipeline = Gst.parse_launch(
        f"appsrc name=src format=time caps=audio/x-raw,format=F32LE,layout=interleaved,channels=2,rate={rate} ! "
        f"{chain} ! appsink name=out sync=false"
    )
    source, sink = pipeline.get_by_name("src"), pipeline.get_by_name("out")
    pipeline.set_state(Gst.State.PLAYING)
    chunk = array.array("f", [level] * (rate // 10 * 2)).tobytes()
    for index in range(20):
        buffer = Gst.Buffer.new_wrapped(chunk)
        buffer.pts, buffer.duration = index * Gst.SECOND // 10, Gst.SECOND // 10
        source.emit("push-buffer", buffer)
    source.emit("end-of-stream")
    nonzero = total = 0
    try:
        while (sample := sink.emit("try-pull-sample", 10 * Gst.SECOND)) is not None:
            buffer = sample.get_buffer()
            samples = array.array("h", buffer.extract_dup(0, buffer.get_size()))
            total += len(samples)
            nonzero += sum(1 for value in samples if value)
    finally:
        pipeline.set_state(Gst.State.NULL)
    return nonzero, total


def chain_of(description):
    """From the source's first link to the tap's S16 caps; the sink is the test's."""
    chain = description.split(" ! ", 1)[1]
    return chain[: chain.index("channels=1") + len("channels=1")]


def test_each_capture_tap_keeps_float_silence_all_zeros(monkeypatch):
    taps = descriptions(monkeypatch)
    assert len(taps) == 6 and all(tap.startswith("pulsesrc") for tap in taps)
    for tap in taps:
        for rate in (48000, 44100):
            for level in (0.0, 6e-8):
                nonzero, total = converted(chain_of(tap), level, rate)
                assert total > rate, tap
                assert nonzero == 0, f"{nonzero} of {total} samples non-zero at {level} ({rate} Hz): {tap}"


def test_the_default_dither_would_break_silence():
    # The control: the same chain with audioconvert's defaults is not silent.
    nonzero, total = converted(
        "queue ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1", 0.0, 48000
    )
    assert nonzero > total // 10
