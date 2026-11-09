"""One controlled pool experiment, on an unmerged evidence-only branch."""

import json
import os
import platform
import re
import subprocess
import time
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
try:
    for number in range(1, 13):
        for phase, data in [("before", original), ("after", changed)]:
            test.write_bytes(data)
            name = f"{phase}-{number}"
            started = time.monotonic()
            with (out / f"{name}.log").open("w") as log:
                result = subprocess.run(
                    [
                        "build-aux/ux-screenshot.sh",
                        str(test),
                        str(out / f"{name}.png"),
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=100,
                )
            text = (out / f"{name}.log").read_text()
            gaps = {
                case: float(ms)
                for case, ms in re.findall(r"PASS (\w+/\w+): mapped, max heartbeat gap ([0-9.]+) ms", text)
            }
            results.append(
                dict(
                    phase=phase,
                    number=number,
                    returncode=result.returncode,
                    seconds=time.monotonic() - started,
                    gaps=gaps,
                )
            )
            (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(results[-1]), flush=True)
            if result.returncode or len(gaps) != 9:
                raise SystemExit(f"{name} failed; measurement preserved, experiment stops for diagnosis")
finally:
    test.write_bytes(changed)
