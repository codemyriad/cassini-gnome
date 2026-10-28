# SPDX-License-Identifier: GPL-3.0-or-later
import runpy
from pathlib import Path

assess = runpy.run_path(str(Path(__file__).parent / "whistle/golden_check.py"))["assess_words"]


def test_d11_3_accepts_at_most_one_frame_only_with_identical_inputs():
    expected = [dict(text="word", startMs=100, endMs=200)]
    shifted = [dict(text="word", startMs=180, endMs=280)]
    hashes = ["a" * 64]
    assert assess(shifted, expected, hashes, hashes)["accepted"]
    assert not assess(shifted, expected, ["b" * 64], hashes)["accepted"]
    assert not assess(shifted, expected, [], [])["accepted"]
    assert not assess([dict(text="word", startMs=181, endMs=281)], expected, hashes, hashes)["accepted"]
    assert not assess([dict(text="different", startMs=100, endMs=200)], expected, hashes, hashes)["accepted"]
    assert not assess([], expected, hashes, hashes)["accepted"]
    assert assess(expected, expected, hashes, hashes)["exactWords"]


def test_d11_5_only_accepts_the_hash_bound_word_and_segment_text(tmp_path):
    import copy
    import hashlib
    import json

    checker = runpy.run_path(str(Path(__file__).parent / "whistle/golden_check.py"))
    contract = checker["load_text_exceptions"]()
    rule = contract["exceptions"][0]
    root = Path(__file__).parent / "whistle/pipeline"
    raw = (root / rule["golden"]).read_bytes()
    golden = json.loads(raw)
    hashes = ["a" * 64] * len(golden["segments"])
    hashes[rule["segmentIndex"]] = rule["segmentInputSha256"]
    engine = dict(
        version=contract["enginePins"]["engineVersion"],
        model=contract["enginePins"]["modelSha256"],
        library=contract["enginePins"]["linuxX86_64LibneedleSha256"],
    )
    words, segments = copy.deepcopy(golden["words"]), copy.deepcopy(golden["segments"])
    words[37]["text"] = "ask"
    segments[5]["text"] = rule["actualSegmentText"]

    def check(w=words, s=segments, data=raw, name=rule["golden"], actual_hashes=hashes, identity=engine):
        return checker["assess_pipeline"](w, s, data, name, actual_hashes, hashes, identity)

    result = check()
    assert result["accepted"] and result["exceptionApplied"] and result["textConforms"]
    assert not result["wordTextMatches"] and not result["exactWords"] and not result["exactSegments"]
    assert result["segmentsConform"]
    assert check(golden["words"], golden["segments"])["accepted"]
    assert not check(name="pipeline-golden.json")["accepted"]
    assert not check(data=json.dumps(golden).encode())["accepted"]
    assert not check(identity=dict(engine, library="wrong"))["accepted"]
    assert not check(actual_hashes=["b" * 64] * len(hashes))["accepted"]
    altered = copy.deepcopy(words)
    altered[36]["text"] = "another"
    assert not check(w=altered)["accepted"]
    altered = copy.deepcopy(words)
    altered[37]["text"] = "asks"
    assert not check(w=altered)["accepted"]
    altered = copy.deepcopy(words)
    altered[37]["startMs"] += 81
    assert not check(w=altered)["accepted"]
    altered = copy.deepcopy(segments)
    altered[5]["startSample"] += 1
    assert not check(s=altered)["accepted"]
    altered = copy.deepcopy(segments)
    altered[5]["text"] += " extra"
    assert not check(s=altered)["accepted"]
    assert not check(w=words[:-1])["accepted"]
    assert not check(w=golden["words"])["accepted"]
    exception_file = Path(__file__).parent / "whistle/whistle-text-exceptions.json"
    assert hashlib.sha256(exception_file.read_bytes()).hexdigest() == checker["TEXT_EXCEPTIONS_SHA256"]
    bad = tmp_path / "bad.json"
    bad.write_bytes(exception_file.read_bytes() + b" ")
    import pytest

    with pytest.raises(ValueError, match="SHA256"):
        checker["load_text_exceptions"](bad)
