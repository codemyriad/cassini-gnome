# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 exact C ABI and token grouping/timing, independent of the speech engine."""

import ctypes as C
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cassini_viewer import parakeet_abi
from cassini_viewer.parakeet import round_ms, words_from_tokens

HERE = Path(__file__).parent / "parakeet"


def test_shared_parakeet_reference_bytes_and_header_are_pinned():
    for name, sha in json.loads((HERE / "UPSTREAM.json").read_text())["sha256"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == sha
    assert (
        hashlib.sha256((HERE / "c-api.h").read_bytes()).hexdigest()
        == "426db2c6acfb51e02143aece67c45779fae699d961c7c26ccf6f1388fdeaa2df"
    )
    assert (HERE / "pins.json").read_bytes() == (
        HERE.parent.parent / "src/cassini_viewer/sherpa-pins.json"
    ).read_bytes()


def test_all_used_struct_sizes_and_every_field_offset_match_the_approved_fork_header(tmp_path):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("A C compiler is required in the explicit native-engine conformance job")
    structs = [value for name, value in vars(parakeet_abi).items() if name.startswith("SherpaOnnx")]
    source = ["#include <stddef.h>", "#include <stdio.h>", '#include "c-api.h"', "int main(void) {"]
    expected = {}
    for struct in structs:
        name = struct.__name__
        expected[name] = C.sizeof(struct)
        source.append(f'printf("{name} %zu\\n", sizeof({name}));')
        for field, _kind in struct._fields_:
            expected[name + "." + field] = getattr(struct, field).offset
            source.append(f'printf("{name}.{field} %zu\\n", offsetof({name}, {field}));')
    source.append("return 0; }")
    c = tmp_path / "abi.c"
    c.write_text("\n".join(source))
    executable = tmp_path / "abi"
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Werror", "-I", str(HERE), str(c), "-o", str(executable)], check=True
    )
    actual = dict(line.split() for line in subprocess.check_output([str(executable)], text=True).splitlines())
    assert {key: int(value) for key, value in actual.items()} == expected


def test_tokens_drop_only_first_boundary_character_and_punctuation_does_not_extend_words():
    result = words_from_tokens(
        [" Hello", "s", ",", "\u2581world", "!", " ,"],
        [0.0, 0.08, 1.0, 1.28, 2.4, 3.2],
        [0.08, 0.08, 0.8, 0.16, 0.8, 0.8],
    )
    assert result == [
        dict(startMs=0, endMs=160, text="Hellos,"),
        dict(startMs=1280, endMs=1440, text="world!"),
        dict(startMs=3200, endMs=3200, text=","),
    ]
    assert words_from_tokens(["  Twice"], [0.0], [0.08])[0]["text"] == " Twice"
    assert words_from_tokens([], [], []) == []


def test_unicode_letters_and_numbers_extend_a_word_but_punctuation_only_does_not():
    result = words_from_tokens([" été", "٢", "…"], [0.0, 0.16, 1.0], [0.08, 0.08, 2.0])
    assert result == [dict(startMs=0, endMs=240, text="été٢…")]


@pytest.mark.parametrize("value,expected", [(0.0005, 1), (-0.0005, -1), (1.2505, 1251), (0.07999999821186066, 80)])
def test_milliseconds_round_half_away_from_zero_from_promoted_float32(value, expected):
    assert round_ms(value) == expected


@pytest.mark.parametrize(
    "tokens,times,durations", [([" Hi"], [], []), (["Hi"], [float("nan")], [0.0]), (["Hi"], [0.0], [-1.0])]
)
def test_invalid_native_arrays_fail(tokens, times, durations):
    with pytest.raises(ValueError):
        words_from_tokens(tokens, times, durations)


def golden_events():
    raw = (HERE / "parakeet-golden.json").read_bytes()
    golden = json.loads(raw)
    progress, cursor = [], 0
    for segment in golden["segments"]:
        count = len(segment["text"].split())
        progress.append(
            dict(
                segment=dict(segment),
                inputSha256=segment["inputSha256"],
                words=[dict(w) for w in golden["words"][cursor : cursor + count]],
            )
        )
        cursor += count
    assert cursor == len(golden["words"])
    return raw, progress


def assess_golden(progress, raw):
    import runpy

    return runpy.run_path(str(HERE / "golden_check.py"))["assess"](progress, raw, "parakeet-golden.json")


def test_conformance_only_allows_timing_after_input_hashes_and_segments_match():
    raw, events = golden_events()
    assert assess_golden(events, raw)["accepted"]
    events[0]["words"][0]["startMs"] += 80
    assert assess_golden(events, raw)["accepted"]
    events[0]["words"][0]["startMs"] += 1
    assert not assess_golden(events, raw)["accepted"]
    events[0]["inputSha256"] = "0" * 64
    result = assess_golden(events, raw)
    assert not result["accepted"] and "maxBoundaryDeltaMs" not in result


@pytest.mark.parametrize(
    "change,accepted",
    [(lambda s: s + ",", True), (lambda s: s.lower(), False), (lambda s: s + "…", False), (lambda s: s + "x", False)],
)
def test_parakeet_conformance_keeps_letters_case_and_non_ascii_punctuation_strict(change, accepted):
    raw, events = golden_events()
    events[0]["words"][0]["text"] = change(events[0]["words"][0]["text"])
    events[0]["segment"]["text"] = " ".join(w["text"] for w in events[0]["words"])
    assert assess_golden(events, raw)["accepted"] is accepted


def test_conformance_never_uses_the_whistle_as_ask_exception_or_draft_parakeet_exceptions():
    import runpy

    compare = runpy.run_path(str(HERE / "golden_check.py"))["punctuation_match"]
    assert not compare("ask", "as") and not compare("you!!", "you")
    assert not compare("U.S.", "US.") and compare("you,", "you.")


def test_model_pins_match_only_the_approved_revision_and_raw_files():
    from types import SimpleNamespace

    from cassini_viewer.parakeet_model import FILES, MODEL_ID, REVISION, check_manifest

    files = [
        SimpleNamespace(
            path=name, artifact=SimpleNamespace(uncompressed_size=size, uncompressed_sha256=sha, size=download)
        )
        for name, (size, sha, download) in FILES.items()
    ]
    model = SimpleNamespace(id=MODEL_ID, source_sha256=REVISION, files=files)
    assert check_manifest(model) is model
    files[0].artifact.uncompressed_sha256 = "0" * 64
    with pytest.raises(ValueError, match="D16"):
        check_manifest(model)
