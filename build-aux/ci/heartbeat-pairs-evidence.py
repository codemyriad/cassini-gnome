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
try:
    for number in range(1, 13):
        for phase, data in [("before", original), ("after", changed)]:
            text = data.decode().replace("import builtins  # noqa: E402", "import json\nimport builtins  # noqa: E402")
            text = text.replace(
                "                gaps, ticks = [], [time.monotonic()]",
                """                stage="opening-setup"
                cpu_ticks=[time.thread_time()]
                beat_info,frames,paint=[],[],[None]
                prior_stage=[stage]
                clock=window.get_frame_clock()
                def before_paint(*_):
                    paint[0]=(time.monotonic(),time.thread_time(),stage)
                def after_paint(*_):
                    if paint[0]:
                        wall,cpu,frame_stage=paint[0]
                        frames.append(dict(wall=time.monotonic()-wall,cpu=time.thread_time()-cpu,stage=frame_stage))
                paint_signals=[clock.connect("before-paint",before_paint),clock.connect("after-paint",after_paint)]
                gaps, ticks = [], [time.monotonic()]""",
            )
            text = text.replace(
                "                    gaps.append(now - ticks[0])",
                """                    cpu=time.thread_time()
                    beat_info.append(dict(stages=[prior_stage[0],stage],cpu=cpu-cpu_ticks[0]))
                    cpu_ticks[0]=cpu
                    prior_stage[0]=stage
                    gaps.append(now - ticks[0])""",
            )
            text = text.replace(
                "                until(dialog.get_mapped, 0.5)",
                '                until(dialog.get_mapped, 0.5)\n                stage="observing"',
            )
            text = text.replace(
                "                dialog.force_close()",
                '                stage="closing-setup"\n                dialog.force_close()',
            )
            text = text.replace(
                "                    settings.force_close()",
                '                    stage="closing-preferences"\n                    settings.force_close()',
            )
            text = text.replace(
                "                assert gaps and max(gaps) < 0.1,",
                """                for signal in paint_signals:
                    clock.disconnect(signal)
                worst=gaps.index(max(gaps))
                print("HEARTBEAT-DIAG "+json.dumps(dict(state=state,entry=entry,gap=max(gaps),beat=beat_info[worst],beats=len(gaps),frames=len(frames),worstFrame=max(frames,key=lambda f:f["wall"]) if frames else None)),flush=True)
                assert gaps and max(gaps) < 0.1,""",
            )
            compile(text, phase, "exec")
            test.write_text(text)
            name = f"{phase}-{number}"
            with (out / f"{name}.log").open("w") as log:
                result = subprocess.run(
                    ["build-aux/ux-screenshot.sh", str(test), str(out / f"{name}.png")],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=100,
                )
            log = (out / f"{name}.log").read_text()
            diagnostics = [
                json.loads(line.removeprefix("HEARTBEAT-DIAG "))
                for line in log.splitlines()
                if line.startswith("HEARTBEAT-DIAG ")
            ]
            expected_budget_failure = bool(
                re.search(
                    r"AssertionError: \('(waiting|failed|needsBrowser)', '(meeting|preferences|library)', [0-9.]+\)",
                    log,
                )
            )
            results.append(
                dict(
                    phase=phase,
                    number=number,
                    returncode=result.returncode,
                    budgetFailure=expected_budget_failure,
                    diagnostics=diagnostics,
                )
            )
            (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(results[-1]), flush=True)
            if result.returncode and not expected_budget_failure:
                raise SystemExit(f"{name}: unexpected non-budget failure, stop for diagnosis")
finally:
    test.write_bytes(changed)
raise SystemExit(int(any(r["returncode"] for r in results)))
