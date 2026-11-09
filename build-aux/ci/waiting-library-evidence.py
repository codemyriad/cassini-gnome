# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed alternating before/after pairs; keep every budget failure."""

import json
import os
import platform
import re
import subprocess
from pathlib import Path

out = Path("heartbeat-evidence").resolve()
out.mkdir()
head = os.environ.get("FRAME_EVIDENCE_HEAD") or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
library = Path("_build-heartbeat/native-profile.so").resolve()
subprocess.run(
    ["cc", "-shared", "-fPIC", "-O2", "build-aux/ci/native-heartbeat-profile.c", "-o", str(library), "-lrt"],
    check=True,
)
(out / "machine.json").write_text(
    json.dumps(
        dict(
            evidenceHead=head,
            baseline="b0b9806",
            runner=os.environ.get("RUNNER_NAME"),
            cpus=os.cpu_count(),
            lscpu=subprocess.check_output(["lscpu"], text=True),
            meminfo=Path("/proc/meminfo").read_text(),
            platform=platform.platform(),
            versions=subprocess.check_output(
                ["dpkg-query", "-W", "libgtk-4-1", "libadwaita-1-0", "libcairo2", "libpango-1.0-0"], text=True
            ),
        ),
        indent=2,
    )
    + "\n"
)
for name in ["libgtk-4.so.1", "libadwaita-1.so.0", "libcairo.so.2", "libpango-1.0.so.0"]:
    with (out / (name + ".elf.txt")).open("w") as target:
        subprocess.run(["readelf", "-n", "/usr/lib/x86_64-linux-gnu/" + name], stdout=target, check=True)

test = Path("tests/ui/companion-click.py")
original = test.read_bytes()
text = original.decode().replace(
    "import builtins  # noqa: E402",
    "import ctypes, json\n"
    "native = ctypes.CDLL(os.environ['FRAME_NATIVE_LIB']) if os.environ.get('FRAME_NATIVE_LIB') else None\n"
    "native_count = native.profile_count if native else lambda: 0\n"
    "def phase(value):\n"
    "    if native: native.profile_phase(value)\n"
    "import builtins  # noqa: E402",
)
text = text.replace(
    "                gaps, ticks = [], [time.monotonic()]",
    """                stage='opening-setup'
                phase(1)
                cpu_ticks=[time.thread_time()]
                samples=[0]
                beat_info, frames, paint=[],[],[None]
                clock=window.get_frame_clock()
                def before_paint(*_):
                    paint[0]=(time.monotonic(),time.thread_time(),stage,native_count())
                def after_paint(*_):
                    if paint[0]:
                        wall,cpu,label,start=paint[0]
                        frames.append(dict(wall=time.monotonic()-wall,cpu=time.thread_time()-cpu,
                                           stage=label,startSample=start,endSample=native_count()))
                signals=[clock.connect('before-paint',before_paint),clock.connect('after-paint',after_paint)]
                if native: native.profile_start()
                gaps, ticks = [], [time.monotonic()]""",
)
text = text.replace(
    "                    gaps.append(now - ticks[0])",
    """                    cpu=time.thread_time()
                    count=native_count()
                    beat_info.append(dict(stage=stage,cpu=cpu-cpu_ticks[0],startSample=samples[0],endSample=count))
                    samples[0]=count
                    cpu_ticks[0]=cpu
                    gaps.append(now - ticks[0])""",
)
text = text.replace(
    "                until(dialog.get_mapped, 0.5)",
    "                until(dialog.get_mapped, 0.5)\n                stage='observing'\n                phase(2)",
)
text = text.replace(
    "                dialog.force_close()",
    "                stage='closing-setup'\n                phase(3)\n                dialog.force_close()",
)
text = text.replace(
    "                    settings.force_close()",
    "                    stage='closing-preferences'\n                    phase(4)\n                    settings.force_close()",
)
text = text.replace(
    "                assert gaps and max(gaps) < 0.1,",
    """                for signal in signals: clock.disconnect(signal)
                native_path=os.path.join(os.environ['FRAME_OUTPUT'],f'{state}-{entry}.native.log')
                if native: native.profile_stop(native_path.encode())
                worst=gaps.index(max(gaps))
                print('FRAME-DIAG '+json.dumps(dict(state=state,entry=entry,gap=max(gaps),
                      beat=beat_info[worst],beats=len(gaps),nativeFile=native_path if native else None,
                      worstFrame=max(frames,key=lambda f:f['wall']) if frames else None)),flush=True)
                assert gaps and max(gaps) < 0.1,""",
)
compile(text, str(test), "exec")
page = Path("src/cassini_talk/meeting_page.py")
candidate_page = page.read_text()
original_page = subprocess.check_output(["git", "show", "b0b9806:src/cassini_talk/meeting_page.py"], text=True)
assert candidate_page != original_page
results = []
try:
    test.write_text(text)
    for pair in range(1, int(os.environ.get("FRAME_CAPTURE_PAIRS", "4")) + 1):
        for mode in ["before", "after"] if pair % 2 else ["after", "before"]:
            page.write_text(original_page if mode == "before" else candidate_page)
            subprocess.run(["meson", "compile", "-C", "_build-heartbeat"], check=True)
            label = f"{mode}-{pair}"
            folder = out / label
            folder.mkdir()
            env = dict(
                os.environ,
                CASSINI_TEST_BUILD=str(Path("_build-heartbeat").resolve()),
                FRAME_NATIVE_LIB=str(library) if mode == "profile" else "",
                FRAME_OUTPUT=str(folder),
            )
            with (out / (label + ".log")).open("w") as log:
                result = subprocess.run(
                    ["build-aux/ux-screenshot.sh", str(test), str(out / (label + ".png"))],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=100,
                )
            output = (out / (label + ".log")).read_text()
            diagnostics = [
                json.loads(line.removeprefix("FRAME-DIAG "))
                for line in output.splitlines()
                if line.startswith("FRAME-DIAG ")
            ]
            budget = bool(
                re.search(
                    r"AssertionError: \('(waiting|failed|needsBrowser)', '(meeting|preferences|library)', [0-9.]+\)",
                    output,
                )
            )
            row = dict(
                pair=pair, mode=mode, returncode=result.returncode, budgetFailure=budget, diagnostics=diagnostics
            )
            results.append(row)
            (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(row), flush=True)
            if result.returncode and not budget:
                raise SystemExit(label + ": unexpected failure; stop for diagnosis")
finally:
    test.write_bytes(original)
    page.write_text(candidate_page)
raise SystemExit(int(any(r["returncode"] for r in results)))
