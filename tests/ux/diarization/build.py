#!/usr/bin/env python3
"""Build the S-11 LibriSpeech golden input with Python's standard library only.

The original CC-BY-4.0 utterances are fetched from an immutable WAV mirror.
No recognizer, model library, audio device, codec or external command is used.
"""

import argparse
import array
import hashlib
import io
import json
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(cache, output, *, offline=False):
    pins = json.loads((HERE / "fetch-pins.json").read_text())
    cache.mkdir(parents=True, exist_ok=True)
    sources = {}
    for source in pins["files"]:
        path = cache / source["filename"]
        if not path.exists():
            if offline:
                raise FileNotFoundError(path)
            with urllib.request.urlopen(source["url"], timeout=60) as response:
                data = response.read(source["bytes"] + 1)
            if len(data) != source["bytes"] or sha(data) != source["sha256"]:
                raise ValueError("Fetched source differs from pin: " + source["id"])
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            temporary.replace(path)
        data = path.read_bytes()
        if len(data) != source["bytes"] or sha(data) != source["sha256"]:
            raise ValueError("Cached source differs from pin: " + source["id"])
        with wave.open(io.BytesIO(data), "rb") as stream:
            if (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
            ) != (1, 2, 16000):
                raise ValueError("Source must already be mono PCM16 at 16 kHz")
            samples = array.array("h", stream.readframes(stream.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        sources[source["id"]] = samples
    mixed = [0] * pins["frames"]
    for row in pins["placements"]:
        source = sources[row["source"]]
        start, count, offset = row["sourceStartFrame"], row["frames"], row["startFrame"]
        if (
            not 0 <= start <= len(source) - count
            or not 0 <= offset <= len(mixed) - count
        ):
            raise ValueError("Fixture placement exceeds pinned audio")
        for index in range(count):
            # Truncate the integer half-gain toward zero, including negatives.
            value = source[start + index]
            scaled = abs(value) * pins["gainNumerator"] // pins["gainDenominator"]
            mixed[offset + index] += -scaled if value < 0 else scaled
    if any(not -32768 <= value <= 32767 for value in mixed):
        raise ValueError("Golden mix clips")
    pcm = array.array("h", mixed)
    if sys.byteorder != "little":
        pcm.byteswap()
    data = pcm.tobytes()
    raw = io.BytesIO()
    with wave.open(raw, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(data)
    wav = raw.getvalue()
    if pins.get("pcmSHA256") and sha(data) != pins["pcmSHA256"]:
        raise ValueError("Generated PCM differs from the frozen hash")
    if pins.get("wavSHA256") and sha(wav) != pins["wavSHA256"]:
        raise ValueError("Generated WAV differs from the frozen hash")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(wav)
    return {
        "frames": len(mixed),
        "durationMs": len(mixed) * 1000 // 16000,
        "pcmSHA256": sha(data),
        "wavSHA256": sha(wav),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(build(args.cache, args.output, offline=args.offline), sort_keys=True)
    )
