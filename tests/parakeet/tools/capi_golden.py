"""D16 harness, C-API path: D11 segments (GNOME code, read-only) -> decode binary (sherpa C API) -> words.

Usage: python -I capi_golden.py --decoder BIN --input WAV --model DIR --vad ONNX --gnome-src DIR
                                [--threads N] [--kind whistle|pipeline] [--resampled F32]
                                [--qemu CPU] [--out JSON] [--raw JSON] [--segdir DIR]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parakeet_golden import words_from_tokens  # noqa: E402

RATE = 16000


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    for name in ("--decoder", "--input", "--model", "--vad", "--gnome-src"):
        ap.add_argument(name, required=True)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--kind", choices=["whistle", "pipeline"], default="whistle")
    ap.add_argument("--resampled")
    ap.add_argument("--qemu")
    ap.add_argument("--out")
    ap.add_argument("--raw")
    a = ap.parse_args()

    sys.path.insert(0, a.gnome_src)
    import numpy as np
    from cassini_viewer.speech_pipeline import SpeechDetector, resampled_chunks, segment_ranges

    detector = SpeechDetector(a.vad)
    ranges = segment_ranges(a.input, detector)
    check = None
    if a.resampled:
        full = np.concatenate(list(resampled_chunks(a.input)))
        ref = np.fromfile(a.resampled, dtype="<f4")
        check = float(np.max(np.abs(full - ref))) if len(full) == len(ref) else "length mismatch"
    with tempfile.TemporaryDirectory() as tmp:
        files, hashes = [], []
        for i, seg in enumerate(ranges):
            samples = np.ascontiguousarray(
                np.concatenate(list(resampled_chunks(a.input, seg["startSample"], seg["endSample"]))), dtype="<f4")
            hashes.append(hashlib.sha256(samples.tobytes()).hexdigest())
            f = Path(tmp) / f"{i:02d}.f32"
            samples.tofile(f)
            files.append(str(f))
        cmd = ([ "qemu-x86_64", "-cpu", a.qemu] if a.qemu else []) + [a.decoder, a.model, str(a.threads), *files]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode:
            sys.exit(f"decoder failed ({proc.returncode}): {proc.stderr[-500:]}")
        results = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(results) == len(ranges)
    segments, words, raw = [], [], []
    for seg, h, r in zip(ranges, hashes, results):
        base = seg["startSample"] * 1000 // RATE
        w = words_from_tokens(r["tokens"], r["timestamps"], r["durations"], base)
        text = r["text"].strip()
        assert " ".join(x["text"] for x in w) == " ".join(text.split()), (text, w)
        segments.append(dict(startSample=seg["startSample"], endSample=seg["endSample"], reason=seg["reason"],
                             text=text, inputSha256=h))
        words.extend(w)
        raw.append(dict(startSample=seg["startSample"], inputSha256=h, **r))
    out = dict(version=1, note="", input=Path(a.input).name, inputSha256=sha(a.input))
    if a.kind == "whistle":
        out["sampleRate"] = RATE
    out.update(segments=segments, words=words)
    if a.kind == "pipeline":
        out.update(resampled="pipeline-resampled-16k.f32", resampledFormat="raw little-endian float32, mono, 16000 Hz",
                   resampledSha256=sha(a.resampled) if a.resampled else None)
    out["_harness"] = dict(decoder=a.decoder, qemu=a.qemu, threads=a.threads, resampledMaxAbsDiff=check,
                           vadRuntime=detector.runtime)
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    if a.raw:
        Path(a.raw).write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(dict(segments=len(segments), words=len(words), resampled=check)))
    for s in segments:
        print(s["startSample"], s["endSample"], s["inputSha256"][:12], s["text"])


if __name__ == "__main__":
    main()
