# SPDX-License-Identifier: GPL-3.0-or-later
"""G13/G14: every job and every local test run has a time budget, and CI runs on pushes to main only."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
BOUND = 'timeout --kill-after=30 "$limit" '
# GNU timeout 0 never times out: a limit must be whole seconds, from 1 to 999999999.
LIMIT_CHECK = '[[ $limit =~ ^0*[1-9][0-9]{0,8}$ ]] || { echo "CASSINI_RUN_TIMEOUT must be whole seconds, from 1 to 999999999" >&2; exit 2; }'
ENTRYPOINT_CHECK = "docker image inspect -f '{{json .Config.Entrypoint}}' "
LIMIT = re.compile(r"^limit=\$\{CASSINI_RUN_TIMEOUT:-\d+\}\n" + re.escape(LIMIT_CHECK) + "$", re.M)


def jobs(workflow):
    body = workflow.read_text().split("\njobs:\n", 1)[1]
    for block in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + body):
        if match := re.match(r"\s*([A-Za-z0-9_-]+):\n", block):
            yield match.group(1), block


def test_every_job_has_a_timeout():
    # GitHub's default is 360 min: a hung job holds a runner for six hours.
    # A reusable-workflow call takes the called jobs' own budgets.
    missing = [
        f"{workflow.name}:{name}"
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        if "\njobs:\n" in workflow.read_text()
        for name, block in jobs(workflow)
        if not re.search(r"^    uses: ", block, re.M) and not re.search(r"^    timeout-minutes: \d+", block, re.M)
    ]
    assert missing == []


def commands(script):
    """A shell script's commands, continuation lines joined."""
    return re.sub(r"\\\n\s*", " ", script.read_text()).splitlines()


def test_every_local_test_run_is_bounded():
    # E1: outside CI nothing caps a run, and a hung UI test lived for 4.5 days.
    # The bound sits inside the container: killing the docker client leaves the
    # container running, and a shell as PID 1 ignores TERM.
    scripts = sorted([*ROOT.glob("tests/**/*.sh"), *ROOT.glob("build-aux/*.sh")])
    unbounded = []
    for script in scripts:
        for line in commands(script):
            if re.search(r"(^|\s)docker run\s", line) and " -d " not in line and BOUND not in line:
                unbounded.append(f"{script.relative_to(ROOT)}: {line.strip()[:80]}")
    assert unbounded == []
    # On the host, bounded-run.sh also kills what the run leaves behind, setsid'd or not.
    host = re.compile(r'("\$bounded"|build-aux/bounded-run\.sh) "\$limit" dbus-run-session ')
    for name in ("build-aux/ux-screenshot.sh", "tests/recording/run.sh"):
        lines = [line for line in commands(ROOT / name) if "xvfb-run -a" in line]
        assert lines and all(host.match(line) for line in lines), name
    for script in scripts:
        text = script.read_text()
        if script.name != "bounded-run.sh" and (BOUND in text or "bounded-run.sh" in text):
            assert LIMIT.search(text), script
    # timeout must be the container's PID 1: when it exits, the kernel kills every
    # process left in the container, setsid'd ones too. The images used by a bounded
    # docker run declare no ENTRYPOINT (ubuntu:26.04 declares none either); the
    # detached audio server's own timeout entrypoint is the exception.
    images = {}
    for script in ROOT.glob("tests/*/run.sh"):
        for line in commands(script):
            if re.search(r"(^|\s)docker build\s", line):
                images.update(dict.fromkeys(re.findall(r"tests/[\w/.-]*(?:Dockerfile|Containerfile)\b", line), script))
    assert set(images) > {"tests/whistle/Containerfile", "tests/desktop/Dockerfile"}
    # Nor their bases: ubuntu:26.04, or one picked at build time (FROM ${BASE}),
    # whose run.sh refuses the built image if it inherited an ENTRYPOINT.
    for name, script in images.items():
        for base in re.findall(r"^FROM\s+(\S+)", (ROOT / name).read_text(), re.M | re.I):
            assert base == "ubuntu:26.04" or ("$" in base and ENTRYPOINT_CHECK in script.read_text()), (name, base)
    entrypoints = [name for name in sorted(images) if re.search(r"^ENTRYPOINT", (ROOT / name).read_text(), re.M | re.I)]
    assert entrypoints == ["tests/recording/Audio.Dockerfile"]
    audio = [line for line in commands(ROOT / "tests/recording/run.sh") if "docker run " in line]
    assert audio and all(" -d " in line for line in audio)
    # The detached audio server is removed by run.sh's EXIT trap; its own bound is the backstop.
    assert (
        'ENTRYPOINT ["timeout", "--kill-after=30", "7200", ' in (ROOT / "tests/recording/Audio.Dockerfile").read_text()
    )


def test_ci_runs_on_pushes_to_main_only():
    trigger = re.search(r"^  push:\n    branches: (.+)$", (WORKFLOWS / "ci.yml").read_text(), re.M)
    assert trigger and trigger.group(1) == "[main]"


def test_release_metadata_runs_in_lint_and_release_preparation_has_no_pr_trigger():
    # G16: one check per PR in Lint (always runs), no path-filtered workflow of its own.
    lint = dict(jobs(WORKFLOWS / "source.yml"))["lint-and-test"]
    assert "build-aux/ci/release_metadata.py --tag" in lint
    release = (WORKFLOWS / "release-check.yml").read_text()
    trigger = release.split("\njobs:", 1)[0]
    assert "pull_request" not in trigger and "workflow_dispatch:" in trigger


# Runner selection: the CM_RUNNER / CM_RUNNER_DESKTOP repository variables move a job to the
# cm-hetzner runners (Ubuntu 26.04 job containers) only when set to exactly "hetzner". The labels
# are a constant; the variable is never parsed, so any other value (unset, empty, a typo, JSON)
# keeps the pinned GitHub image instead of failing the expression or queueing for missing labels.
CM_HETZNER = ["self-hosted", "cm-hetzner"]
RUNNER_SELECTOR = re.compile(
    r"^\$\{\{ vars\.(?P<var>CM_RUNNER(?:_DESKTOP)?) == 'hetzner' && "
    r"fromJSON\('\[\"self-hosted\",\"cm-hetzner\"\]'\) \|\| 'ubuntu-26\.04' \}\}$"
)
CM_RUNNER_PINNED = (
    "${{ vars.CM_RUNNER == 'hetzner' && fromJSON('[\"self-hosted\",\"cm-hetzner\"]') || 'ubuntu-26.04' }}"
)


def select_runner(runs_on, variables):
    """What GitHub picks for a runs-on value, given the repository variables (a model of the
    expression above: `==` compares strings case-insensitively, an unset variable is '')."""
    match = RUNNER_SELECTOR.match(runs_on)
    if match is None:
        return runs_on
    return CM_HETZNER if (variables.get(match["var"]) or "").casefold() == "hetzner" else "ubuntu-26.04"


def runs_on_values():
    """(workflow, runs-on) for every job; a block runs-on (a YAML list or mapping) is joined into one string."""
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        lines = workflow.read_text().splitlines()
        for i, line in enumerate(lines):
            if match := re.match(r"^    runs-on:(.*)$", line):
                value = [match.group(1).strip()]
                for following in lines[i + 1 :]:
                    if not following.startswith("      "):
                        break
                    value.append(following.strip())
                yield workflow.name, "\n".join(v for v in value if v)


def test_package_builds_run_on_the_pinned_runner_image():
    # G16: the package builds run on the image every other job pins, not on
    # whatever ubuntu-latest points to. release.yml's publish job only uploads.
    for name in ("deb.yml", "packages.yml", "rpm-canary.yml"):
        images = re.findall(r"^    runs-on: (.+)$", (WORKFLOWS / name).read_text(), re.M)
        assert images and set(images) <= {"ubuntu-26.04", CM_RUNNER_PINNED}, (name, images)


def test_release_ships_the_flatpak_main_tested_and_refuses_it_without_an_update_remote():
    # G15: no rebuild at tag time, and no workflow calls Release preparation.
    release = (WORKFLOWS / "release.yml").read_text()
    flatpak = dict(jobs(WORKFLOWS / "release.yml"))["flatpak"]
    assert "release-check.yml" not in release and "name: flatpak-stable" in flatpak
    assert "run-id: ${{ needs.main-inputs.outputs.run_id }}" in flatpak
    assert "build-aux/ci/flatpak_bundle.py dist/cassini-talk.flatpak" in flatpak
    assert re.search(r"^    needs: \[deb, packages, flatpak\]$", release, re.M)
    assert not any("release-check.yml" in w.read_text() for w in WORKFLOWS.glob("*.yml"))
    stable = (WORKFLOWS / "flatpak.yml").read_text()
    assert "--repo-url" not in stable and "build-aux/ci/validate_installed.py" in stable
    assert "name: flatpak-stable" in stable


def test_runner_variables_only_select_constant_labels():
    # A variable that reaches runs-on must go through the allowlisted selector: never
    # fromJSON(vars.X) or any other form that parses or forwards the variable's value.
    for name, value in runs_on_values():
        if "vars." in value:
            assert RUNNER_SELECTOR.match(value), (name, value)
            assert "fromJSON(vars" not in value, (name, value)


def test_runner_selection_falls_back_to_github_for_every_other_value():
    moved = [(name, value) for name, value in runs_on_values() if "vars." in value]
    assert moved, "no job reads a runner variable"
    for name, value in moved:
        var = RUNNER_SELECTOR.match(value)["var"]
        for setting in (
            None,
            "",
            " ",
            "garbage{",
            '["self-hosted","cm-hetzner"]',
            '["self-hosted","cm-hetzner-typo"]',
            "hetzner-typo",
            "true",
            "github",
        ):
            variables = {} if setting is None else {var: setting}
            assert select_runner(value, variables) == "ubuntu-26.04", (name, setting)
        for setting in ("hetzner", "Hetzner"):
            assert select_runner(value, {var: setting}) == CM_HETZNER, (name, setting)
        # One variable never moves the other variable's jobs.
        other = "CM_RUNNER_DESKTOP" if var == "CM_RUNNER" else "CM_RUNNER"
        assert select_runner(value, {other: "hetzner"}) == "ubuntu-26.04", (name, other)


def test_runner_variables_move_the_intended_jobs():
    expected = {
        "CM_RUNNER": {"build.yml", "deb.yml", "packages.yml", "flatpak.yml", "source.yml", "rpm-canary.yml"},
        "CM_RUNNER_DESKTOP": {"recording.yml", "ux-shot.yml", "whistle.yml"},
    }
    found = {"CM_RUNNER": set(), "CM_RUNNER_DESKTOP": set()}
    for name, value in runs_on_values():
        if match := RUNNER_SELECTOR.match(value):
            found[match["var"]].add(name)
    assert found == expected
    # Release workflows always run on GitHub-hosted runners.
    for name, value in runs_on_values():
        if name.startswith("release"):
            assert "vars." not in value, (name, value)


def test_every_runs_on_is_a_hosted_image_or_the_selector():
    # Nothing else may reach runs-on: no literal self-hosted labels, no variable in a block-style runs-on.
    hosted = re.compile(r"^(?:ubuntu|macos|windows)-[0-9a-z.-]+$")
    for name, value in runs_on_values():
        assert hosted.match(value) or RUNNER_SELECTOR.match(value), (name, value)
