#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D38 real production queue, unchanged PCM and measured display output offline."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_viewer.library import LibraryScanner  # noqa: E402
from cassini_viewer.transcription import TranscriptionQueue, track_speaker  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
assert {name for _, name in socket.if_nameindex()} == {"lo"}
root = Path(__file__).resolve().parent
args.output.mkdir(parents=True, exist_ok=True)
library = args.output / "Library"
os.environ["CASSINI_LIBRARY_DIR"] = str(library)
subprocess.run([sys.executable, str(root / "make.py"), str(library)], check=True)
pins = json.loads((root / "UPSTREAM.json").read_text())["sha256"]
for name, digest in pins.items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in library.rglob("*.wav")}
raw = {}


class ObservedClient(WhistleClient):
    def process_tracks(self, tracks, progress):
        words = []

        def observe(value):
            if value["type"] == "progress":
                speaker = track_speaker(tracks[value["trackOrder"]])[0]
                words.extend(dict(w, speaker=speaker, _order=value["trackOrder"]) for w in value["words"])
            progress(value)

        result = super().process_tracks(tracks, observe)
        raw[Path(tracks[0]["path"]).parent] = [
            {k: v for k, v in w.items() if k != "_order"}
            for w in sorted(words, key=lambda w: (w["startMs"], w["_order"]))
        ]
        return result


finished, results = [], []
queue = TranscriptionQueue(lambda _: None, finished.append, bundle=args.bundle, client_factory=ObservedClient)
try:
    for name in ("clean", "bleed"):
        suffix = "1" if name == "clean" else "2"
        folder = library / ("Recording-b1eed000-0000-4000-8000-00000000000" + suffix)
        started = time.monotonic()
        assert queue.enqueue(folder)
        while folder not in finished and time.monotonic() - started < 240:
            time.sleep(0.05)
        assert folder in finished, f"{name} queue timed out"
        meeting = next(m for m in LibraryScanner(library).scan().meetings if m.directory == folder)
        assert meeting.transcript == "done", (name, meeting.transcript, meeting.error)
        body = json.loads((folder / "transcript.json").read_text())
        if name == "clean":
            assert body["items"] == raw[folder], "Clean control changed after recognition"
            assert all(not w.get("echo") for w in body["items"])
        visible = dict(body, items=[w for w in body["items"] if w.get("echo") is not True])
        display_file = args.output / f"{name}-display.transcript.json"
        display_file.write_text(json.dumps(visible))
        score = subprocess.check_output(
            [sys.executable, str(root / "measure.py"), str(display_file), "--turns"], text=True
        )
        print(score, flush=True)
        (args.output / f"{name}-measure.txt").write_text(score)
        echo = int(re.search(r"\bme:\s+\d+ right, (\d+) echo", score)[1])
        turns = int(re.search(r"(\d+) displayed turns", score)[1])
        assert echo <= 2 and turns <= 10, (name, echo, turns)
        # Scanner is the source for UI, Search, export and speaker metrics.
        assert [w.text for w in meeting.words] == [w["text"] for w in visible["items"]]
        result = dict(
            case=name,
            words=len(body["items"]),
            flagged=sum(w.get("echo") is True for w in body["items"]),
            visibleWords=len(visible["items"]),
            echoWords=echo,
            displayedTurns=turns,
            seconds=round(time.monotonic() - started, 3),
            cleanUnchanged=name == "clean",
        )
        results.append(result)
        (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
finally:
    queue.close()
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in library.rglob("*.wav")}
print("PASS D38 real queue: clean unchanged, bleed <=2 echo words and <=10 displayed turns; WAVs unchanged")
