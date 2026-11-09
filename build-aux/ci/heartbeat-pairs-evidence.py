"""One controlled pool experiment, on an unmerged evidence-only branch."""

import json
import os
import platform
import subprocess
from pathlib import Path

out = Path("heartbeat-evidence")
out.mkdir()
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
original = subprocess.check_output(["git", "show", "e60cd5d:tests/ui/companion-click.py"])
test = Path("tests/ui/companion-click.py")
changed = test.read_bytes()
(out / "machine.json").write_text(
    json.dumps(
        dict(
            evidenceHead=head,
            testHead="4ceb4687df3ebff9f778fd8a86d5188839116360",
            baseline="e60cd5d",
            runner=os.environ.get("RUNNER_NAME"),
            cpus=os.cpu_count(),
            machine=platform.machine(),
            lscpu=subprocess.check_output(["lscpu"], text=True),
            meminfo=Path("/proc/meminfo").read_text(),
        ),
        indent=2,
    )
    + "\n"
)
results = []
env = dict(os.environ, CASSINI_TEST_BUILD=str(Path("_build-heartbeat").resolve()))
subprocess.run(
    [
        "cc",
        "-shared",
        "-fPIC",
        "-O2",
        "build-aux/ci/native-heartbeat-profile.c",
        "-o",
        "/tmp/heartbeat-profile.so",
        "-lrt",
    ],
    check=True,
)
versions = subprocess.check_output(["dpkg-query", "-W", "libgtk-4-1", "libadwaita-1-0"], text=True)
(out / "versions.txt").write_text(
    versions + subprocess.check_output(["readelf", "-n", "/usr/lib/x86_64-linux-gnu/libgtk-4.so.1"], text=True)
)
try:
    for phase, data in [("before", original), ("after", changed)]:
        text = data.decode().replace(
            "import builtins  # noqa: E402",
            'import ctypes\nnative_profile=ctypes.CDLL("/tmp/heartbeat-profile.so")\nimport builtins  # noqa: E402',
        )
        text = text.replace(
            "                gaps, ticks = [], [time.monotonic()]",
            '                stage="opening"\n                stages, prior_stage = [], [stage]\n                gaps, ticks = [], [time.monotonic()]',
        )
        text = text.replace(
            "                    gaps.append(now - ticks[0])",
            "                    stages.append((prior_stage[0],stage))\n                    prior_stage[0]=stage\n                    gaps.append(now - ticks[0])",
        )
        text = text.replace(
            "                timer = GLib.timeout_add(10, beat)",
            "                native_profile.profile_phase(0)\n                native_profile.profile_start()\n                timer = GLib.timeout_add(10, beat)",
        )
        text = text.replace(
            "                until(dialog.get_mapped, 0.5)",
            '                until(dialog.get_mapped, 0.5)\n                stage="observing"\n                native_profile.profile_phase(1)',
        )
        text = text.replace(
            "                dialog.force_close()",
            '                stage="closing-setup"\n                native_profile.profile_phase(2)\n                dialog.force_close()',
        )
        text = text.replace(
            "                    settings.force_close()",
            '                    stage="closing-preferences"\n                    native_profile.profile_phase(3)\n                    settings.force_close()',
        )
        text = text.replace(
            "                assert gaps and max(gaps) < 0.1,",
            '                native_profile.profile_stop(f"'
            + str(out.resolve())
            + "/"
            + phase
            + '-{state}-{entry}.native.log".encode())\n                assert gaps and max(gaps) < 0.1,',
        )
        text = text.replace("(state, entry, max(gaps))", "(state, entry, max(gaps), stages[gaps.index(max(gaps))])")
        test.write_text(text)
        with (out / f"{phase}-profile.log").open("w") as log:
            result = subprocess.run(
                ["build-aux/ux-screenshot.sh", str(test), str(out / f"{phase}-profile.png")],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=100,
            )
        results.append(dict(phase=phase, returncode=result.returncode, profile=True))
        (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results[-1]), flush=True)
finally:
    test.write_bytes(changed)
raise SystemExit(int(any(r["returncode"] for r in results)))
