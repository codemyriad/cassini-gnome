#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run unchanged pinned Android algorithms on S-9; do not touch its checkout.

Provide the Kotlin compiler's own dependency classpath separately from the
application runtime classpath (Kotlin stdlib and JVM org.json). No fetching.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-repo", type=Path, required=True)
    parser.add_argument("--compiler-classpath", required=True)
    parser.add_argument("--runtime-classpath", required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows-output", type=Path, required=True)
    args = parser.parse_args()
    provenance = json.loads((ROOT / "vectors.json").read_text())["provenance"]
    pins = {p["path"]: p["sha256"] for p in provenance["files"]}
    commit = provenance["commit"]

    def source(path):
        data = subprocess.check_output(["git", "-C", str(args.android_repo), "show", f"{commit}:{path}"])
        assert hashlib.sha256(data).hexdigest() == pins[path], path
        return data

    with tempfile.TemporaryDirectory(prefix="cassini-s9-reference-") as tmp:
        tmp = Path(tmp)
        names = [
            "VoiceprintSelection",
            "VoiceMatcher",
            "VoiceCodec",
            "NameMatch",
            "VoiceStaleness",
            "NoteVoiceCodec",
            "SpeakerBudget",
        ]
        paths = [f"core/src/main/kotlin/org/cassini/core/speakers/{n}.kt" for n in names]
        paths += ["core/src/main/kotlin/org/cassini/core/models/ModelCatalog.kt"]
        files = []
        for path in paths:
            dest = tmp / Path(path).name
            dest.write_bytes(source(path))
            files.append(dest)
        # Data-only declarations are copied verbatim; the rest of their files
        # depend on unrelated format code. Algorithms above are never edited.
        for name, path, package in [
            ("Word", "core/src/main/kotlin/org/cassini/core/format/Transcript.kt", "format"),
            ("SpeakerTurn", "core/src/main/kotlin/org/cassini/core/speakers/SpeakerAssignment.kt", "speakers"),
        ]:
            line = next(v for v in source(path).decode().splitlines() if v.startswith(f"data class {name}("))
            dest = tmp / f"{name}.kt"
            dest.write_text(f"package org.cassini.core.{package}\n{line}\n")
            files.append(dest)
        jar = tmp / "oracle.jar"
        subprocess.run(
            [
                args.java,
                "-cp",
                args.compiler_classpath,
                "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                "-no-stdlib",
                "-no-reflect",
                "-classpath",
                args.runtime_classpath,
                "-d",
                str(jar),
                *map(str, files),
                str(ROOT / "AndroidOracle.kt"),
            ],
            check=True,
        )
        subprocess.run(
            [
                args.java,
                "-cp",
                os.pathsep.join([str(jar), args.runtime_classpath]),
                "AndroidOracleKt",
                str(ROOT / "vectors.json"),
                str(args.output.resolve()),
                str(ROOT / "../diarization/golden.json"),
                str(args.windows_output.resolve()),
            ],
            check=True,
        )
    print("PASS unchanged pinned Android algorithms; checkout untouched")


if __name__ == "__main__":
    main()
