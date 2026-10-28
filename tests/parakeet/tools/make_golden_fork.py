import hashlib, json, sys
from pathlib import Path
W = Path(sys.argv[1]); F = W / "fork"
pin = json.loads((W / "models/parakeet-ultra-0.6b-int8.pin.json").read_text())
lib = F / "src/sherpa-onnx-go-linux/lib/x86_64-unknown-linux-gnu"
h = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
engine = {"name": "sherpa-onnx (codemyriad fork)", "tag": "v1.13.7-cassini.8",
          "nativeVersion": "1.13.7+cassini-parakeet-v3-reference-v1.nemotron-diarization-v2.parakeet-ultra-v1",
          "source": "codemyriad/sherpa-onnx 0c2ee3f3f5f16bb06d234a0f98632810296ba0eb (v1.13.7-cassini-parakeet-ultra-v1)",
          "api": "C API", "goldenPlatform": "x86_64-unknown-linux-gnu (onnxruntime 1.27.1)",
          "libs": {"libsherpa-onnx-c-api.so": h(lib / "libsherpa-onnx-c-api.so"), "libonnxruntime.so": h(lib / "libonnxruntime.so")}}
model = {"distId": pin["id"], "sourceSha256": pin["source_sha256"], "files": {f["path"]: f["uncompressed_sha256"] for f in pin["files"]},
         "transform": "none: the dist files are loaded exactly as downloaded"}
config = {"model_type": "nemo_transducer", "decoding_method": "greedy_search", "num_threads": 1, "provider": "cpu",
          "sample_rate": 16000, "feature_dim": 128, "dither": 0.0, "blank_penalty": 0.0}
rule = ("Word times = segment start (floor ms) + per-word ms from token timestamps: a token beginning with U+2581 or U+0020 starts a word; "
        "start = round(ts*1000) of the word's first piece; end = max round((ts+dur)*1000) over pieces holding a Unicode letter or number "
        "(punctuation stays attached but never extends the word); round half away from zero, in double from the engine's float32.")
names = {"seg-t1-r1.json": ("parakeet-golden.json", "whistle"), "pipe-t1-r1.json": ("parakeet-pipeline-golden.json", "pipeline")}
for src, (dst, kind) in names.items():
    g = json.loads((F / "runs" / src).read_text()); g.pop("_harness"); g.pop("note")
    note = ("D16 golden (fork engine), same input and D11 segmentation as " + ("whistle-golden.json" if kind == "whistle" else "pipeline-golden.json (48 kHz -> cassini-fir-48k-16k-v1)") +
            ", segment boundaries identical; recognised per segment by the unmodified dist parakeet-ultra-0.6b-int8 with codemyriad sherpa-onnx v1.13.7-cassini.8 (C API). " + rule +
            " Conformance: segments, segment inputSha256 and word count exact; words exact up to trailing , . ? ! (D16); startMs/endMs within 80 ms (D11.3) once hashes match.")
    out = {"version": 1, "decision": "D16", "note": note, **g, "engine": engine, "model": model, "recognizerConfig": config}
    (F / "golden" / dst).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
# strict-option evidence: QEMU flips, hash-bound
ex = []
for golden, src, others in [("parakeet-golden.json", "seg-t1-r1.json", ["seg-qemu-Nehalem.json", "seg-qemu-Haswell.json"]),
                            ("parakeet-pipeline-golden.json", "pipe-t1-r1.json", ["pipe-qemu-Nehalem.json", "pipe-qemu-Haswell.json"])]:
    a = json.loads((F / "runs" / src).read_text()); gsha = h(F / "golden" / golden)
    starts = [s["startSample"] * 1000 // 16000 for s in a["segments"]]
    for o in others:
        b = json.loads((F / "runs" / o).read_text())
        for i, (x, y) in enumerate(zip(a["words"], b["words"])):
            if x["text"] != y["text"]:
                k = max(j for j, st in enumerate(starts) if st <= x["startMs"])
                s = a["segments"][k]
                e = next((e for e in ex if e["golden"] == golden and e["wordIndex"] == i), None)
                if e:
                    e["allowedWords"] = sorted(set(e["allowedWords"]) | {y["text"]}); e["evidence"].append(o); continue
                ex.append(dict(golden=golden, goldenSha256=gsha, segmentIndex=k, startSample=s["startSample"], endSample=s["endSample"],
                               segmentInputSha256=s["inputSha256"], wordIndex=i, expectedWord=x["text"], actualWord=y["text"],
                               allowedWords=sorted({x["text"], y["text"]}), evidence=[o]))
doc = dict(version=1, decision="D16 (draft, strict option)", platform="any",
           enginePins=dict(engine="codemyriad sherpa-onnx v1.13.7-cassini.8", linuxCApiSha256=engine["libs"]["libsherpa-onnx-c-api.so"]),
           exceptions=ex, note="Punctuation-only flips from the same fork binaries under qemu-x86_64 (-cpu Nehalem = SSE4.2, -cpu Haswell-noTSX = AVX2). Letters/numbers never changed; segment input hashes identical.")
(F / "golden" / "parakeet-text-exceptions.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
for e in ex: print(e["golden"], "seg", e["segmentIndex"], "word", e["wordIndex"], e["allowedWords"], e["evidence"])
