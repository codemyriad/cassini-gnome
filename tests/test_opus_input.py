# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - GStreamer fixtures follow dependency checks.
"""The inference reader decodes Ogg Opus whatever input rate its OpusHead records (D-1054)."""

import struct
from contextlib import closing

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("The Opus reader needs GStreamer", allow_module_level=True)
from gi.repository import Gst

from cassini_talk.opus_input import pcm_blocks

Gst.init(None)
SECONDS = 2


def encode(path, rate):
    """Ogg Opus from a 440 Hz tone captured at `rate`; opusenc records it as the OpusHead input rate."""
    missing = [name for name in ("audiotestsrc", "opusenc", "oggmux") if Gst.ElementFactory.find(name) is None]
    if missing:
        pytest.skip(f"Missing GStreamer elements: {', '.join(missing)}")
    pipeline = Gst.parse_launch(
        f"audiotestsrc freq=440 volume=0.2 samplesperbuffer={rate // 10} num-buffers={SECONDS * 10} ! "
        f"audio/x-raw,format=S16LE,rate={rate},channels=1 ! opusenc ! oggmux ! filesink name=output"
    )
    pipeline.get_by_name("output").set_property("location", str(path))
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        assert message and message.type == Gst.MessageType.EOS
    finally:
        pipeline.set_state(Gst.State.NULL)
    data = path.read_bytes()
    head = data.index(b"OpusHead")
    return struct.unpack("<I", data[head + 12 : head + 16])[0]


@pytest.mark.parametrize("rate", [16000, 48000])
def test_reader_yields_48_khz_mono_for_any_opushead_input_rate(tmp_path, rate):
    path = tmp_path / f"voice-{rate}.opus"
    assert encode(path, rate) == rate
    with closing(pcm_blocks(path, inactivity_seconds=10)) as blocks:
        decoded = np.concatenate(list(blocks))
    # Every block is 48 kHz mono float32; the tone survives at its level.
    assert decoded.dtype == np.float32
    assert abs(len(decoded) - SECONDS * 48000) < 48000 // 10
    assert 0.05 < float(np.sqrt(np.mean(decoded[4800:-4800] ** 2))) < 0.3
