#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode a two-hour received Opus inside a 64 MiB /tmp, with bounded RSS."""

import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cassini_viewer.speech_pipeline import audio_chunks, audio_segments  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
Gst.init(None)
output = Path(sys.argv[1])
received = output / "two-hour.opus"
pipeline = Gst.parse_launch(
    "audiotestsrc wave=silence num-buffers=7200 samplesperbuffer=48000 ! "
    "audio/x-raw,rate=48000,channels=2 ! audioconvert ! opusenc complexity=0 ! oggmux ! filesink name=out"
)
pipeline.get_by_name("out").set_property("location", str(received))
try:
    pipeline.set_state(Gst.State.PLAYING)
    message = pipeline.get_bus().timed_pop_filtered(180 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
    assert message and message.type == Gst.MessageType.EOS
finally:
    pipeline.set_state(Gst.State.NULL)
track = dict(path=str(received), format="opus")
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
started = time.monotonic()
segments = [dict(startSample=1000, endSample=161000), dict(startSample=7190 * 16000, endSample=7200 * 16000)]
references = [[], []]
count = maximum = 0
for chunk in audio_chunks(track):
    for index, segment in enumerate(segments):
        start, end = max(count, segment["startSample"]), min(count + len(chunk), segment["endSample"])
        if start < end:
            references[index].append(chunk[start - count : end - count])
    count += len(chunk)
    maximum = max(maximum, len(chunk))
assert count == 7200 * 16000, count
assert maximum <= 16000
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
assert peak - before < 96 * 1024, (before, peak)
# Sequential recognition can skip a large gap and keeps only bounded segments.
# The second pass must produce identical samples at the same positions.
proof = list(audio_segments(track, segments))
assert [len(v) for v in proof] == [160000, 160000]
assert all(np.array_equal(actual, np.concatenate(expected)) for actual, expected in zip(proof, references, strict=True))
assert not list(Path("/tmp").glob("cassini-transcribe-*"))
report = dict(
    durationSeconds=7200,
    decodedSamples=count,
    maximumChunkSamples=maximum,
    peakRssKiB=peak,
    incrementalPeakRssKiB=peak - before,
    seconds=time.monotonic() - started,
    compressedBytes=received.stat().st_size,
    noTemporaryWav=True,
    sequentialSegmentSkip=True,
    segmentInputSha256=[hashlib.sha256(v.tobytes()).hexdigest() for v in proof],
)
(output / "stream-linux.json").write_text(json.dumps(report, indent=2) + "\n")
# Re-exporting a long received recording must preserve its compressed packets,
# stereo shape and bounded memory, without another decoder or a /tmp WAV.
from cassini_viewer import portable  # noqa: E402
from cassini_viewer.exporting import export_meeting  # noqa: E402
from cassini_viewer.open_meeting import load_open_meeting  # noqa: E402

export_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
export_start = time.monotonic()
model = load_open_meeting(received, output / "Library")
with received.open("rb") as stream:
    digest = portable.compute_audio_digest(stream)
exported = export_meeting(model, output / "Documents")
parsed = portable.read_meeting(str(exported))
assert portable.verify_audio(str(exported), parsed).state == portable.State.OK
assert parsed.channels == 2 and parsed.manifest["integrity"]["opusAudioSha256"] == digest["sha256"]
assert parsed.manifest["audio"]["sampleCount"] == 7200 * 48000
assert not list(Path("/tmp").rglob("*.wav"))
export_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
assert export_peak - export_before < 96 * 1024
export_report = dict(
    durationSeconds=7200,
    compressedBytes=exported.stat().st_size,
    seconds=time.monotonic() - export_start,
    channels=parsed.channels,
    unchangedOpusSha256=digest["sha256"],
    noTemporaryWav=True,
    incrementalPeakRssKiB=export_peak - export_before,
)
(output / "export-long-linux.json").write_text(json.dumps(export_report, indent=2) + "\n")
exported.unlink()
received.unlink()
print(json.dumps(report, indent=2))
