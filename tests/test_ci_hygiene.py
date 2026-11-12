# SPDX-License-Identifier: GPL-3.0-or-later
"""G13/G14: every job and every local test run has a time budget, and CI runs on pushes to main only."""

import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_no_job_or_step_hides_its_failure():
    # D66/R93: a red job is root-fixed, never hidden. continue-on-error would let a
    # failing job (shared test 2's live-folder, gating since R93) pass its workflow.
    hidden = [
        f"{workflow.name}:{line.strip()}"
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for line in workflow.read_text().splitlines()
        if re.match(r"\s*(-\s+)?continue-on-error:", line)
    ]
    assert hidden == []


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
            assert re.fullmatch(r"ubuntu:26\.04@sha256:[0-9a-f]{64}", base) or (
                "$" in base and ENTRYPOINT_CHECK in script.read_text()
            ), (name, base)
    entrypoints = [name for name in sorted(images) if re.search(r"^ENTRYPOINT", (ROOT / name).read_text(), re.M | re.I)]
    assert entrypoints == ["tests/recording/Audio.Dockerfile"]
    audio = [line for line in commands(ROOT / "tests/recording/run.sh") if "docker run " in line]
    assert audio and all(" -d " in line for line in audio)
    # The detached audio server is removed by run.sh's EXIT trap; its own bound is the backstop.
    assert (
        'ENTRYPOINT ["timeout", "--kill-after=30", "7200", ' in (ROOT / "tests/recording/Audio.Dockerfile").read_text()
    )


def test_recording_evidence_uploads_keep_hidden_library_folders():
    # GNOME keeps a recording under Library/.cassini/sessions/Recording-*, and
    # upload-artifact leaves out hidden files by default: the evidence would lose
    # its session.json and WAVs.
    steps = [
        block
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for block in re.split(r"\n(?=      - )", workflow.read_text())
        if "actions/upload-artifact@" in block and "tests/recording/artifacts" in block
    ]
    assert steps
    assert all(re.search(r"^          include-hidden-files: true$", block, re.M) for block in steps)


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
# cm-hetzner runners (Ubuntu 26.04 job containers) only on allowlisted events (push, schedule,
# workflow_dispatch, and same-repository pull_request) and only when set to exactly "hetzner".
# The labels are a constant; the variable is never parsed, so any other value (unset, empty, a
# typo, JSON), any fork pull request, or any unallowlisted event (pull_request_target,
# workflow_run, issue_comment, etc.) keeps the pinned GitHub image instead of running untrusted
# code on self-hosted runners, failing the expression, or queueing forever.
CM_HETZNER = ["self-hosted", "cm-hetzner"]
EVENT_ALLOWLIST = (
    r"\(github\.event_name == 'push' \|\| github\.event_name == 'schedule' \|\| "
    r"github\.event_name == 'workflow_dispatch' \|\| "
    r"\(github\.event_name == 'pull_request' && "
    r"github\.event\.pull_request\.head\.repo\.full_name == github\.repository\)\)"
)
RUNNER_SELECTOR = re.compile(
    rf"^\$\{{\{{ {EVENT_ALLOWLIST} && vars\.(?P<var>CM_RUNNER(?:_DESKTOP)?) == 'hetzner' && "
    r"fromJSON\('\[\"self-hosted\",\"cm-hetzner\"\]'\) \|\| 'ubuntu-26\.04' \}\}$"
)
CM_RUNNER_PINNED = (
    "${{ (github.event_name == 'push' || github.event_name == 'schedule' || "
    "github.event_name == 'workflow_dispatch' || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository)) && "
    "vars.CM_RUNNER == 'hetzner' && fromJSON('[\"self-hosted\",\"cm-hetzner\"]') || 'ubuntu-26.04' }}"
)


class _CIString(str):
    """GitHub Actions string comparison (`==` / `!=`) is case-insensitive."""

    def __eq__(self, other):
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __ne__(self, other):
        return not self.__eq__(other)


class _NullContext:
    """GitHub Actions dereferences missing/null context properties to null instead of raising."""

    def __getattr__(self, _name):
        return self

    def __bool__(self):
        return False


_NULL = _NullContext()


def select_runner(
    runs_on,
    variables,
    *,
    event_name="push",
    head_repo="codemyriad/cassini-gnome",
    repository="codemyriad/cassini-gnome",
):
    """Evaluate a job's runs-on expression under GitHub Actions event, repo, and vars contexts."""
    if not (runs_on.startswith("${{") and runs_on.endswith("}}")):
        return runs_on
    expr = runs_on.removeprefix("${{").removesuffix("}}").strip()
    expr = expr.replace("&&", " and ").replace("||", " or ")
    repo_obj = _NULL if head_repo is None else SimpleNamespace(full_name=_CIString(head_repo))
    event = SimpleNamespace(pull_request=SimpleNamespace(head=SimpleNamespace(repo=repo_obj)))
    github = SimpleNamespace(
        event_name=_CIString(event_name),
        repository=_CIString(repository),
        event=event if event_name in ("pull_request", "pull_request_target") else _NULL,
    )
    vars_ns = SimpleNamespace(
        CM_RUNNER=_CIString(variables.get("CM_RUNNER") or ""),
        CM_RUNNER_DESKTOP=_CIString(variables.get("CM_RUNNER_DESKTOP") or ""),
    )
    return eval(expr, {"__builtins__": {}}, {"github": github, "vars": vars_ns, "fromJSON": json.loads})


def job_runs_on_values():
    """(workflow, job, runs-on) for every job with a runs-on key; block forms are joined."""
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        if "\njobs:\n" not in text:
            continue
        for job_name, block in jobs(workflow):
            lines = block.splitlines()
            for i, line in enumerate(lines):
                if match := re.match(r"^    runs-on:(.*)$", line):
                    value = [match.group(1).strip()]
                    for following in lines[i + 1 :]:
                        if not following.startswith("      "):
                            break
                        value.append(following.strip())
                    yield workflow.name, job_name, "\n".join(v for v in value if v)


def runs_on_values():
    """(workflow, runs-on) for every job; a block runs-on (a YAML list or mapping) is joined into one string."""
    for workflow_name, _job_name, value in job_runs_on_values():
        yield workflow_name, value


def test_package_builds_run_on_the_pinned_runner_image():
    # G16: the package builds run on the image every other job pins, not on
    # whatever ubuntu-latest points to. release.yml's publish job only uploads.
    for name in ("deb.yml", "packages.yml", "rpm-canary.yml"):
        images = re.findall(r"^    runs-on: (.+)$", (WORKFLOWS / name).read_text(), re.M)
        assert images and set(images) <= {"ubuntu-26.04", CM_RUNNER_PINNED}, (name, images)


# A step that publishes to a GitHub release, whatever the tool: the gh CLI, the
# REST API (gh api, curl to /releases or uploads.github.com) or an action.
RELEASE_UPLOAD = re.compile(
    r"\bgh release\b|/releases\b|uploads\.github\.com|^\s*(?:- )?uses: *[^\s#@]*release[^\s#@]*@",
    re.I | re.M,
)


def release_started(text):
    # True when a release, a ref creation or a tag push can start the workflow.
    # A push with no branch filter fires on tags too; one with only branch
    # filters does not.
    on = re.search(r"^on:(.*)\n((?:[ \t].*\n|\n)*)", text, re.M)
    if not on:
        return False
    if re.search(r"\b(?:release|create)\b", on.group(1)) or re.search(r"^  (?:release|create):", on.group(2), re.M):
        return True
    if re.search(r"\bpush\b", on.group(1)):
        return True
    push = re.search(r"^  push:(.*)\n((?:    .*\n|\n)*)", on.group(2), re.M)
    if not push:
        return False
    filters = push.group(1) + push.group(2)
    return bool(re.search(r"\btags", filters)) or not re.search(r"\bbranches", filters)


def test_release_ships_the_native_packages_and_never_a_flatpak():
    # D45.4: no Flatpak is published anywhere; we hold no signing key and run
    # no repository, and CI builds the Flatpak only as a check. A release ships the native packages
    # (publish needs exactly deb and packages), never downloads main's
    # flatpak-stable and never attaches a .flatpak: a bundle without an update
    # remote must not be offered. No workflow calls Release preparation.
    def code(block):
        return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))

    release = dict(jobs(WORKFLOWS / "release.yml"))
    assert set(release) == {"main-inputs", "deb", "packages", "publish"}
    assert re.findall(r"^    needs: (.+)$", release["publish"], re.M) == ["[deb, packages]"]
    assert re.findall(r"^          name: (\S+)$", release["publish"], re.M) == ["release-deb", "release-packages"]
    # Every download selects its artifacts by a name or pattern that cannot
    # reach main's Flatpak: no download-everything.
    for name, block in release.items():
        for step in re.split(r"\n(?=      - )", code(block)):
            if "actions/download-artifact" in step:
                chosen = re.findall(r"^          (?:name|pattern): *[\"']?([^\"'\s]+)", step, re.M)
                assert len(chosen) == 1, (name, step)
                assert not fnmatch.fnmatchcase("flatpak-stable", chosen[0]), (name, chosen)
    # Shipping jobs: every job of a workflow a tag, release or ref creation
    # starts, of the workflows those call, and every job anywhere that
    # publishes to a GitHub release by any means.
    by_name = {w.name: w for w in WORKFLOWS.glob("*.yml") if "\njobs:\n" in w.read_text()}
    released = sorted(w for w, path in by_name.items() if release_started(path.read_text()))
    assert "release.yml" in released, released
    shipping = {}
    while released:
        workflow = released.pop()
        for name, block in jobs(by_name[workflow]):
            if f"{workflow}:{name}" not in shipping:
                shipping[f"{workflow}:{name}"] = code(block)
                released += re.findall(r"^    uses: \./\.github/workflows/(\S+\.yml)", block, re.M)
    for workflow, path in by_name.items():
        for name, block in jobs(path):
            if RELEASE_UPLOAD.search(code(block)):
                shipping.setdefault(f"{workflow}:{name}", code(block))
    assert "release.yml:publish" in shipping
    for name, block in shipping.items():
        assert "flatpak-stable" not in block and ".flatpak" not in block.lower(), name
        assert "release-flatpak" not in block, name
    assert not any("release-check.yml" in w.read_text() for w in WORKFLOWS.glob("*.yml"))
    # Main still builds, validates and smoke-tests the stable profile Flathub
    # will build, without an update remote.
    stable = (WORKFLOWS / "flatpak.yml").read_text()
    assert "--repo-url" not in stable and "build-aux/ci/validate_installed.py" in stable
    assert "name: flatpak-stable" in stable


def test_release_upload_pattern_sees_every_publishing_route():
    for line in (
        "gh release upload v1 x.deb",
        "gh api -X POST repos/o/r/releases/1/assets --input x",
        "curl -T x https://uploads.github.com/repos/o/r/releases/1/assets?name=x",
        "      - uses: softprops/action-gh-release@v2",
        "      - uses: actions/upload-release-asset@v1",
        "      - uses: ncipollo/release-action@v1",
    ):
        assert RELEASE_UPLOAD.search(line), line
    for line in (
        "      - uses: actions/upload-artifact@v7",
        "      - uses: actions/download-artifact@v8",
        "    uses: ./.github/workflows/flatpak.yml",
        "python3 build-aux/ci/release_metadata.py --tag v1",
    ):
        assert not RELEASE_UPLOAD.search(line), line
    for on in (
        'on:\n  push:\n    tags: ["v*"]\n',
        "on:\n  push:\n",
        "on:\n  push:\n    paths: [src/**]\n",
        "on:\n  push: {tags: [v*]}\n",
        "on:\n  push:\n    branches: [main]\n    tags: [v*]\n",
        "on:\n  release:\n    types: [published]\n",
        "on:\n  create:\n",
        "on: [push, pull_request]\n",
        "on: release\n",
    ):
        assert release_started(on + "jobs:\n"), on
    for on in (
        "on:\n  push:\n    branches: [main]\n  pull_request:\n",
        "on:\n  push: {branches: [main]}\n",
        "on:\n  workflow_call:\n  workflow_dispatch:\n",
        "on:\n  schedule:\n    - cron: '1 2 * * *'\n",
    ):
        assert not release_started(on + "jobs:\n"), on


def test_runner_variables_only_select_constant_labels():
    # A variable that reaches runs-on must go through the allowlisted selector: never
    # fromJSON(vars.X) or any other form that parses or forwards the variable's value.
    for name, value in runs_on_values():
        if "vars." in value:
            assert RUNNER_SELECTOR.match(value), (name, value)
            assert "fromJSON(vars" not in value, (name, value)


def test_runner_selection_falls_back_to_github_for_every_other_value_and_for_fork_prs():
    moved = [(wf, job, value) for wf, job, value in job_runs_on_values() if "vars." in value]
    assert moved, "no job reads a runner variable"
    for wf, job, value in moved:
        where = f"{wf}:{job}"
        var = RUNNER_SELECTOR.match(value)["var"]
        for event_name, head_repo in (
            ("push", None),
            ("schedule", None),
            ("workflow_dispatch", None),
            ("pull_request", "codemyriad/cassini-gnome"),
        ):
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
                assert select_runner(value, variables, event_name=event_name, head_repo=head_repo) == "ubuntu-26.04", (
                    where,
                    event_name,
                    setting,
                )
            for setting in ("hetzner", "Hetzner"):
                assert select_runner(value, {var: setting}, event_name=event_name, head_repo=head_repo) == CM_HETZNER, (
                    where,
                    event_name,
                    setting,
                )
            # One variable never moves the other variable's jobs.
            other = "CM_RUNNER_DESKTOP" if var == "CM_RUNNER" else "CM_RUNNER"
            assert (
                select_runner(value, {other: "hetzner"}, event_name=event_name, head_repo=head_repo) == "ubuntu-26.04"
            ), (where, event_name, other)
        # Fork PRs (and deleted-fork PRs) never run on self-hosted runners even when both variables are set.
        both_enabled = {"CM_RUNNER": "hetzner", "CM_RUNNER_DESKTOP": "hetzner"}
        for untrusted_head in ("fork/cassini-gnome", "codemyriad/other-repo", "", None):
            assert (
                select_runner(value, both_enabled, event_name="pull_request", head_repo=untrusted_head)
                == "ubuntu-26.04"
            ), (where, untrusted_head)
        # Prohibited or unallowlisted events never select self-hosted runners, even for same-repo payloads.
        for prohibited_event in (
            "pull_request_target",
            "workflow_run",
            "issue_comment",
            "release",
            "merge_group",
            "repository_dispatch",
            "",
        ):
            for candidate_head in ("codemyriad/cassini-gnome", "fork/cassini-gnome", None):
                assert (
                    select_runner(value, both_enabled, event_name=prohibited_event, head_repo=candidate_head)
                    == "ubuntu-26.04"
                ), (where, prohibited_event, candidate_head)


def test_runner_variables_move_the_intended_jobs():
    expected = {
        "CM_RUNNER": {
            ("build.yml", "products"),
            ("daily-test-release.yml", "arch"),
            ("daily-test-release.yml", "deb"),
            ("daily-test-release.yml", "rpm"),
            ("deb.yml", "build"),
            ("flatpak.yml", "flatpak"),
            ("meson-tests.yml", "meson-tests"),
            ("packages.yml", "arch"),
            ("packages.yml", "rpm"),
            ("rpm-canary.yml", "preflight"),
            ("rpm-canary.yml", "rpm"),
            ("source.yml", "distros"),
            ("source.yml", "lint-and-test"),
            ("ux-library.yml", "golden-vectors-and-reader"),
        },
        "CM_RUNNER_DESKTOP": {
            ("background-apps.yml", "desktop"),
            ("exact-transcript.yml", "native-gtk"),
            ("recording.yml", "capture"),
            ("recording.yml", "live-folder"),
            ("ux-shot.yml", "current-window"),
            ("whistle.yml", "pinned-offline-recognition"),
        },
    }
    hosted_only = {
        ("cache-hygiene.yml", "prune"),      # holds an actions: write token: never on the pool
        ("ci.yml", "changes"),
        ("ci.yml", "pytest-coverage"),
        ("daily-test-release.yml", "outcome"),
        ("daily-test-release.yml", "plan"),
        ("daily-test-release.yml", "publish"),
        ("release-check.yml", "metadata"),
        ("release.yml", "deb"),
        ("release.yml", "main-inputs"),
        ("release.yml", "packages"),
        ("release.yml", "publish"),
    }
    found = {"CM_RUNNER": set(), "CM_RUNNER_DESKTOP": set()}
    unmoved = set()
    for wf, job, value in job_runs_on_values():
        if match := RUNNER_SELECTOR.match(value):
            found[match["var"]].add((wf, job))
        else:
            unmoved.add((wf, job))
            assert "vars." not in value, (wf, job, value)
    assert found == expected
    assert unmoved == hosted_only
    # Release workflows always run on GitHub-hosted runners.
    for wf, job, value in job_runs_on_values():
        if wf.startswith("release"):
            assert "vars." not in value, (wf, job, value)


def test_every_runs_on_is_a_hosted_image_or_the_selector():
    # Nothing else may reach runs-on: no literal self-hosted labels, no variable in a block-style runs-on.
    hosted = re.compile(r"^(?:ubuntu|macos|windows)-[0-9a-z.-]+$")
    for name, value in runs_on_values():
        assert hosted.match(value) or RUNNER_SELECTOR.match(value), (name, value)


HARNESS_IMAGES = {
    "desktop": "tests/desktop/Dockerfile",
    "build-refresh": "tests/build-refresh/Dockerfile",
    "browser-setup": "tests/browser-setup/Dockerfile",
    "whistle": "tests/whistle/Containerfile",
}


def steps(workflow):
    """Each step of a workflow, as its own text."""
    return re.split(r"\n(?=      - )", workflow.read_text())[1:]


MAIN_ONLY = "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"


def step_field(step, name):
    match = re.search(rf"^\s+{name}: (.+)$", step, re.M)
    return match and match[1].strip()


def test_every_cache_save_is_main_only_and_saves_what_a_restore_read():
    # D66: an explicit actions/cache/save runs on main pushes only, only when its
    # restore missed, and saves exactly the path and key that restore looked up.
    found = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        restores = {}
        for step in steps(workflow):
            if "uses: actions/cache/restore@" in step:
                restores[step_field(step, "id")] = step
            if "uses: actions/cache/save@" not in step:
                continue
            condition = step_field(step, "if") or ""
            assert MAIN_ONLY in condition, (workflow.name, step)
            restore_id = re.fullmatch(r".* && steps\.([\w-]+)\.outputs\.cache-hit != 'true'", condition)[1]
            restore = restores[restore_id]
            assert step_field(step, "path") == step_field(restore, "path"), (workflow.name, step)
            key = step_field(step, "key")
            assert key in (step_field(restore, "key"), f"${{{{ steps.{restore_id}.outputs.cache-primary-key }}}}")
            found.append((workflow.name, restore_id))
    assert sorted(found) == [
        ("background-apps.yml", "harness-cache"),
        ("deb.yml", "harness-cache"),
        ("flatpak.yml", "trash-portal"),
        ("whistle.yml", "harness-cache"),
    ]


def test_harness_image_caches_are_saved_from_main_only():
    # D66: a pull request restores caches and never saves one. The harness
    # images are only ever restored, then saved by an explicit main-only step.
    # The older restore-and-save caches are pinned in D66_SAVE_ON_PR_DEBT below.
    saves, restores = [], []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for step in steps(workflow):
            if ("uses: actions/cache/save@" in step and "steps.harness.outputs.key" in step) or (
                "harness-cache.sh save" in step
            ):
                saves.append(workflow.name)
                assert re.search(r"^        if: .*" + re.escape(MAIN_ONLY), step, re.M), (workflow.name, step)
            if "harness-cache" in step and "uses: actions/cache@" in step:
                raise AssertionError(f"{workflow.name}: harness images through actions/cache, which saves on PRs")
            if "uses: actions/cache/restore@" in step and "steps.harness.outputs.key" in step:
                restores.append(workflow.name)
    assert sorted(restores) == ["background-apps.yml", "deb.yml", "whistle.yml"]
    assert sorted(saves) == sorted(restores * 2)


# D66 debt: these caches restore and save in one step (actions/cache, or the
# flatpak-builder action's cache-key), so a pull request's miss saves an entry.
# Follow-up: split each into actions/cache/restore plus a main-only
# actions/cache/save, then drop it here. Nothing may be added to this list.
D66_SAVE_ON_PR_DEBT = sorted(
    [
        ("background-apps.yml", "desktop-gnome50-v1"),
        *[("build.yml", key) for key in ("whistle", "sherpa")],
        *[("deb.yml", key) for key in ("whistle", "sherpa", "parakeet-ultra-v2", "browser-startup-linux-x64")],
        *[("flatpak.yml", key) for key in ("flatpak-builder-devel", "parakeet-ultra-v2")],
        *[("packages.yml", key) for key in ("whistle", "sherpa", "parakeet-ultra-v2") * 2],
        ("recording.yml", "browser-startup-linux-x64"),
        *[("rpm-canary.yml", key) for key in ("whistle", "sherpa", "parakeet-ultra-v2")],
        *[("ux-shot.yml", key) for key in ("whistle", "sherpa")],
        *[
            ("whistle.yml", key)
            for key in ("whistle", "sherpa", "parakeet-ultra-v2", "nemotron", "campplus", "s11-audio")
        ],
    ]
)


def test_no_new_cache_saves_on_pull_requests():
    # D66: every cache that can save from a pull request is listed debt; a new
    # one fails here instead of passing under the main-only test above.
    found = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for step in steps(workflow):
            if re.search(r"uses: actions/cache@", step) or re.search(r"^\s+cache-key: ", step, re.M):
                key = re.search(r"^\s+(?:key|cache-key): (\S+)", step, re.M)[1]
                found.append((workflow.name, re.match(r"[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*", key)[0]))
    assert sorted(found) == D66_SAVE_ON_PR_DEBT


REFRESH = "RUN apt-get update && apt-get -y --no-install-recommends dist-upgrade"


def test_harness_images_install_the_app_on_top_of_the_cached_stage():
    # C: the cached stage holds the base, the harness packages and the app's static
    # Depends; app.deb goes in on every run, after fresh indexes, so apt resolves
    # the package's own dependencies each time.
    # D68: every run first brings the cached stage up to the current archive, so a
    # week-old harness never pins the product's runtime libraries (GTK, libadwaita,
    # GStreamer) or the distro target to an older archive than an uncached build.
    for name, path in HARNESS_IMAGES.items():
        text = "\n".join(line for line in (ROOT / path).read_text().splitlines() if not line.startswith("#"))
        harness, separator, current = text.partition("\nFROM ${HARNESS}\n")
        assert separator and "ARG HARNESS=harness\n" in harness and "\nFROM ${BASE} AS harness\n" in harness
        assert current.startswith(REFRESH) or f"\n{REFRESH}" in current, name
        assert "dist-upgrade" not in harness, name
        if name == "whistle":
            assert "app.deb" not in text
            continue
        assert (
            "app.deb" not in harness and 'apt-get satisfy -y --no-install-recommends "$(cat /tmp/depends)"' in harness
        )
        assert (
            f"COPY app.deb /tmp/app.deb\n{REFRESH} \\\n    && apt-get install -y --no-install-recommends /tmp/app.deb"
        ) in current, name
    for script in ROOT.glob("tests/*/run.sh"):
        lines = commands(script)
        for name in re.findall(r'tests/harness-image\.sh" tag ([\w-]+)', script.read_text()):
            builds = [line for line in lines if re.search(r"(^|[\s(])docker build\s", line)]
            assert all(HARNESS_IMAGES[name] in line for line in builds), script
            # The harness stage is built only for a new tag, from a freshly pulled base.
            assert any("--pull" in line and "--target harness" in line and '-t "$harness"' in line for line in builds)
            # The per-run stage is rebuilt every run: no local layer cache keeps an old refresh.
            if name == "whistle":
                assert 'refresh-image.sh" build "$harness" "$offline_job"' in script.read_text()
                assert 'refresh-image.sh" check "$CASSINI_OFFLINE_HARNESS_IMAGE"' in script.read_text()
                refresh = commands(ROOT / "tests/whistle/refresh-image.sh")
                current = [line for line in refresh if '--build-arg HARNESS="$image"' in line]
            else:
                current = [line for line in builds if '--build-arg HARNESS="$harness"' in line]
            assert current and all("--no-cache" in line and "--pull" not in line for line in current), script
            # Nothing runs in the cached stage itself.
            runs = [line for line in lines if re.search(r"(^|\s)docker run\s", line)]
            assert runs and not any(re.search(r'"\$harness"\s', line) for line in runs), script


# Pinned bases (test_harness_base.py): no registry lookup, the same base every call.
HARNESS_BASES = " ".join(f"{image}@sha256:{'0' * 64}" for image in ("ubuntu:26.04", "debian:testing"))


def harness_tag(root, name, epoch="2026-W45", **env):
    return subprocess.run(
        ["bash", str(root / "tests/harness-image.sh"), "tag", name],
        env={"PATH": os.environ["PATH"], "CASSINI_HARNESS_EPOCH": epoch, "CASSINI_HARNESS_BASES": HARNESS_BASES, **env},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_harness_image_tag_changes_with_every_input_and_every_epoch(tmp_path):
    # D68: a cached harness never outlives its inputs, nor a week.
    for path in ["tests/harness-image.sh", "debian/control", *HARNESS_IMAGES.values()]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / path, tmp_path / path)
    tags = {name: harness_tag(tmp_path, name) for name in HARNESS_IMAGES}
    assert len(set(tags.values())) == len(tags)
    assert all(re.fullmatch(rf"cassini-harness-{name}:[0-9a-f]{{16}}-2026-W45", tag) for name, tag in tags.items())
    assert harness_tag(tmp_path, "desktop", "2026-W46") != tags["desktop"]
    assert harness_tag(tmp_path, "browser-setup", CASSINI_BROWSER_TEST_BASE="debian:testing") != tags["browser-setup"]
    inputs = {"tests/harness-image.sh": set(HARNESS_IMAGES), "debian/control": set(HARNESS_IMAGES) - {"whistle"}}
    inputs |= {path: {name} for name, path in HARNESS_IMAGES.items()}
    for path, names in inputs.items():
        original = (tmp_path / path).read_text()
        (tmp_path / path).write_text(original + "\n# changed\n")
        assert {name for name in HARNESS_IMAGES if harness_tag(tmp_path, name) != tags[name]} == names, path
        (tmp_path / path).write_text(original)
    with pytest.raises(subprocess.CalledProcessError):
        harness_tag(tmp_path, "desktop", "2026 W45")


def test_the_harness_stage_installs_the_static_runtime_depends():
    control = (ROOT / "debian/control").read_text()
    stanza = control[control.index("\nPackage: cassini-talk\n") :]
    field = re.search(r"^Depends:(.*(?:\n[ \t].*)*)", stanza, re.M)[1]
    expected = [d.strip() for d in field.split(",") if d.strip() and not d.strip().startswith("${")]
    depends = subprocess.run(
        ["bash", str(ROOT / "tests/harness-image.sh"), "depends"], check=True, capture_output=True, text=True
    ).stdout
    assert depends.endswith("\n") and depends.strip().split(", ") == expected
    assert "python3-gi" in expected and "gir1.2-gtk-4.0 (>= 4.18)" in expected


def test_the_meson_tests_run_beside_the_build_products_and_gate_every_run():
    # H: Recording, Boards and Offline wait only for the products; the Meson tests
    # run on every CI run, unfiltered, and nothing passes without them.
    ci = dict(jobs(WORKFLOWS / "ci.yml"))
    assert [line.strip() for line in ci["meson-tests"].strip().splitlines()] == [
        "meson-tests:",
        "name: Meson tests",
        "if: ${{ github.event.action != 'labeled' || github.event.label.name == 'full-ci' }}",
        "uses: ./.github/workflows/meson-tests.yml",
    ]
    for name in ("recording", "boards", "offline"):
        assert re.search(r"^    needs: \[changes, build\]$", ci[name], re.M) and "meson-tests" not in ci[name], name
    assert "needs: [changes, source, deb, rpm-arch, meson-tests]" in ci["pytest-coverage"]
    for var in ("SOURCE", "DEB", "RPM_ARCH", "MESON_TESTS"):
        assert f'test "${var}" = success ||' in ci["pytest-coverage"]
    (tests,) = dict(jobs(WORKFLOWS / "meson-tests.yml")).values()
    products = dict(jobs(WORKFLOWS / "build.yml"))["products"]
    for line in ("meson test -C _build-core --print-errorlogs", "meson compile -C _build-core cassini-talk-pot"):
        assert line in tests and line not in products
    assert re.search(r"container: ubuntu:26\.04@sha256:[0-9a-f]{64}\b", tests) and not re.search(
        r"^    if:", tests, re.M
    )
    for step in re.split(r"^      - ", tests, flags=re.M)[1:]:
        if "if:" in step:
            assert (
                step.startswith("uses: actions/upload-artifact@v7\n")
                and "if: ${{ failure() || (github.event_name != 'pull_request' && always()) }}" in step
            )
    # The same minimal environment as the products' build.
    install = re.compile(r"- name: Install build tools \(minimal source-validation environment\)\n(?:        .*\n)+")
    assert install.search(tests)[0] == install.search(products)[0]


def test_playwright_browser_caches_use_runner_temp_isolation():
    # Self-hosted runners have a different GITHUB_WORKSPACE depth relative to ~
    # than GitHub-hosted runners and may run concurrent jobs under one user;
    # Playwright browsers must live under runner.temp, never ~/.cache.
    cache_jobs = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        assert "~/.cache/ms-playwright" not in text, workflow.name
        if "\njobs:\n" not in text:
            continue
        for name, block in jobs(workflow):
            if "playwright install" in block:
                cache_jobs.append(f"{workflow.name}:{name}")
                env_pos = block.index('PLAYWRIGHT_BROWSERS_PATH=$RUNNER_TEMP/ms-playwright" >> "$GITHUB_ENV"')
                cache_pos = block.index("path: ${{ runner.temp }}/ms-playwright")
                install_pos = block.index("playwright install")
                assert env_pos < install_pos and cache_pos < install_pos, (workflow.name, name)
    assert cache_jobs == ["deb.yml:build", "recording.yml:capture", "recording.yml:live-folder"]
    recording_jobs = dict(jobs(WORKFLOWS / "recording.yml"))
    for name in ("capture", "live-folder"):
        assert "sudo build-aux/ci/apt-install-bounded.sh " in recording_jobs[name], name


def _load_rpm_canary_guard():
    spec = importlib.util.spec_from_file_location(
        "check_rpm_canary_wheel", ROOT / "build-aux/ci/check_rpm_canary_wheel.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rpm_canary_workflow_separates_preflight_so_rpm_build_is_visibly_skipped():
    canary_jobs = dict(jobs(WORKFLOWS / "rpm-canary.yml"))
    assert list(canary_jobs) == ["preflight", "rpm"]
    preflight = canary_jobs["preflight"]
    rpm = canary_jobs["rpm"]
    assert "should_build: ${{ steps.canary-abi.outputs.should_build }}" in preflight
    assert "python3 build-aux/ci/check_rpm_canary_wheel.py" in preflight
    assert "build-aux/rpm/build.sh" not in preflight
    assert "name: Build .rpm (fedora:latest)" in rpm
    assert re.search(r"^    needs: preflight$", rpm, re.M)
    assert re.search(r"^    if: needs\.preflight\.outputs\.should_build == 'true'$", rpm, re.M)
    assert "      - run: build-aux/whistle-cache.sh\n      - run: build-aux/rpm/build.sh fedora:latest" in rpm
    assert not re.search(r"^        if:", rpm, re.M)


def test_rpm_canary_guard_runs_pinned_abi_and_skips_unpinned_until_pypi_ships_wheel(tmp_path, monkeypatch):
    guard = _load_rpm_canary_guard()
    pins = ROOT / "whistle/pins.json"
    spec = ROOT / "build-aux/rpm/cassini-talk.spec"

    # 1. Pinned ABI (cp314) builds immediately without querying PyPI.
    out_pinned = tmp_path / "out_pinned"
    rc = guard.evaluate(
        "cp314",
        pins,
        spec,
        github_output=out_pinned,
        fetcher=lambda: pytest.fail("Pinned ABI must not query PyPI"),
    )
    assert rc == 0 and out_pinned.read_text().strip() == "should_build=true"

    # 2. Unpinned ABI (cp315) with a verified PyPI lookup proving no wheel exists skips cleanly.
    out_skip = tmp_path / "out_skip"
    summary_skip = tmp_path / "summary_skip"
    pypi_without_cp315 = {
        "releases": {
            "1.31.0": [
                {"filename": "onnxruntime-1.31.0-cp314-cp314-manylinux_2_28_x86_64.whl", "yanked": False},
                {"filename": "onnxruntime-1.31.0-cp315-cp315-manylinux_2_28_x86_64.whl", "yanked": True},
            ]
        }
    }
    rc = guard.evaluate(
        "cp315",
        pins,
        spec,
        github_output=out_skip,
        step_summary=summary_skip,
        fetcher=lambda: pypi_without_cp315,
    )
    assert rc == 0 and out_skip.read_text().strip() == "should_build=false"
    assert "no cp315 manylinux x86_64 wheel on PyPI yet" in summary_skip.read_text()

    # 3. Once PyPI ships a non-yanked cp315 wheel, preflight fails loudly to prompt pinning.
    out_ready = tmp_path / "out_ready"
    summary_ready = tmp_path / "summary_ready"
    pypi_with_cp315 = {
        "releases": {
            "1.32.0": [
                {"filename": "onnxruntime-1.32.0-cp315-cp315-manylinux_2_28_x86_64.whl", "yanked": False},
            ]
        }
    }
    rc = guard.evaluate(
        "cp315",
        pins,
        spec,
        github_output=out_ready,
        step_summary=summary_ready,
        fetcher=lambda: pypi_with_cp315,
    )
    assert rc == 1 and out_ready.read_text().strip() == "should_build=false"
    assert "onnxruntime-1.32.0-cp315-cp315-manylinux_2_28_x86_64.whl" in summary_ready.read_text()

    # 4. Pin mismatches between whistle/pins.json and cassini-talk.spec fail preflight in both directions.
    pins_with_cp315 = tmp_path / "pins_mismatch.json"
    pins_data = json.loads(pins.read_text())
    pins_data["onnxruntime"]["wheels"]["cp315"] = {"url": "https://example.invalid/ort.whl", "sha256": "0" * 64}
    pins_with_cp315.write_text(json.dumps(pins_data))
    out_mismatch_a = tmp_path / "out_mismatch_a"
    summary_mismatch_a = tmp_path / "summary_mismatch_a"
    rc = guard.evaluate(
        "cp315",
        pins_with_cp315,
        spec,
        github_output=out_mismatch_a,
        step_summary=summary_mismatch_a,
        fetcher=lambda: pytest.fail("Pin mismatch must fail before querying PyPI"),
    )
    assert rc == 1 and out_mismatch_a.read_text().strip() == "should_build=false"
    assert "pin mismatch" in summary_mismatch_a.read_text()

    spec_with_cp315 = tmp_path / "cassini-talk-mismatch.spec"
    spec_with_cp315.write_text(spec.read_text() + '\n%if "%{?python3_version_nodots}" == "315"\n%endif\n')
    out_mismatch_b = tmp_path / "out_mismatch_b"
    rc = guard.evaluate(
        "cp315",
        pins,
        spec_with_cp315,
        github_output=out_mismatch_b,
        fetcher=lambda: pytest.fail("Pin mismatch must fail before querying PyPI"),
    )
    assert rc == 1 and out_mismatch_b.read_text().strip() == "should_build=false"

    # 5. Bounded retry recovers from HTTP 503 and rejects truncated Content-Length responses.
    payload = json.dumps(pypi_without_cp315).encode("utf-8")
    calls = []

    class _FakeResp:
        def __init__(self, body: bytes, length: int | None):
            self.headers = {} if length is None else {"Content-Length": str(length)}
            self._body = body

        def read(self, limit: int) -> bytes:
            if "Content-Length" in self.headers:
                assert limit == int(self.headers["Content-Length"])
            return self._body[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen_recovers(req, timeout=10.0):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))
        if len(calls) == 2:
            return _FakeResp(payload[:10], len(payload))
        return _FakeResp(payload, len(payload))

    monkeypatch.setattr(guard.urllib.request, "urlopen", fake_urlopen_recovers)
    sleeps = []
    assert guard.fetch_pypi_json(sleep_fn=sleeps.append) == pypi_without_cp315
    assert len(calls) == 3 and sleeps == [2.0, 4.0]

    # 6. Exhausted transient PyPI errors fail preflight (never skip green when availability is unproven).
    exhausted_sleeps = []
    monkeypatch.setattr(
        guard.urllib.request,
        "urlopen",
        lambda req, timeout=10.0: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))
        ),
    )
    out_exhausted = tmp_path / "out_exhausted"
    summary_exhausted = tmp_path / "summary_exhausted"
    rc = guard.evaluate(
        "cp315",
        pins,
        spec,
        github_output=out_exhausted,
        step_summary=summary_exhausted,
        fetcher=lambda: guard.fetch_pypi_json(sleep_fn=exhausted_sleeps.append),
    )
    assert rc == 1 and out_exhausted.read_text().strip() == "should_build=false"
    assert exhausted_sleeps == [2.0, 4.0]
    assert "PyPI wheel check failed" in summary_exhausted.read_text()

    # 7. Malformed PyPI responses (invalid JSON, missing 'releases', oversized Content-Length, HTTP 404) fail preflight.
    for idx, bad_opener in enumerate(
        (
            lambda _req, timeout=10.0: _FakeResp(b"{not-json", 9),
            lambda _req, timeout=10.0: _FakeResp(b'{"info": {}}', 12),
            lambda _req, timeout=10.0: _FakeResp(b"{}", guard.MAX_RESPONSE_BYTES + 5),
            lambda req, timeout=10.0: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
            ),
        )
    ):
        monkeypatch.setattr(guard.urllib.request, "urlopen", bad_opener)
        out_bad = tmp_path / f"out_bad_{idx}"
        summary_bad = tmp_path / f"summary_bad_{idx}"
        rc = guard.evaluate(
            "cp315",
            pins,
            spec,
            github_output=out_bad,
            step_summary=summary_bad,
            fetcher=lambda: guard.fetch_pypi_json(sleep_fn=lambda _s: None),
        )
        assert rc == 1 and out_bad.read_text().strip() == "should_build=false", idx
        assert "PyPI wheel check failed" in summary_bad.read_text(), idx


def test_pr_two_distro_legs_and_full_ci_label_wiring():
    ci_text = (WORKFLOWS / "ci.yml").read_text()
    assert "  pull_request:\n    types: [opened, synchronize, reopened, labeled]\n" in ci_text
    ci_jobs = dict(jobs(WORKFLOWS / "ci.yml"))
    for leg in ("deb_testing", "rpm_opensuse", "arch", "flatpak", "extra_distros", "full_ci"):
        assert f"{leg}: ${{{{ steps.areas.outputs.{leg} }}}}" in ci_jobs["changes"]
    assert (
        "if: ${{ !cancelled() && (github.event.action != 'labeled' || github.event.label.name == 'full-ci') && "
        "(needs.changes.result != 'success' || "
        "(needs.changes.outputs.flatpak != 'false' && needs.changes.outputs.packages != 'false')) }}"
    ) in ci_jobs["flatpak"]
    assert (
        "if: ${{ always() && !cancelled() && needs.deb.result == 'success' && "
        "((needs.changes.result == 'success' && needs.changes.outputs.flatpak == 'false' && "
        "needs.flatpak.result == 'skipped') || needs.flatpak.result == 'success') }}"
    ) in ci_jobs["desktop"]
    assert (
        "include_flatpak: ${{ needs.changes.result != 'success' || needs.changes.outputs.flatpak != 'false' }}"
        in ci_jobs["desktop"]
    )
    bg_text = (WORKFLOWS / "background-apps.yml").read_text()
    assert "tests/desktop/run.sh --deb-only _desktop-packages/deb/cassini-talk_*.deb desktop-evidence" in bg_text


def _load_pin_images():
    spec = importlib.util.spec_from_file_location("pin_images", ROOT / "build-aux/ci/pin_images.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_ci_container_images_are_digest_pinned_and_bump_script_validates(tmp_path):
    pin_images = _load_pin_images()
    assert pin_images.check_repo(ROOT) == []
    assert pin_images.main(["--check"], root=ROOT) == 0

    # Verify image_slug and deb BASE_TAG strip @sha256:... while preserving tag-safe slugs and filenames.
    slug_out = subprocess.run(
        [
            "sh",
            "-ec",
            "CONTAINER_ENGINE=true . build-aux/container.sh\n"
            'image_slug "ubuntu:26.04@sha256:' + "a" * 64 + '"\n'
            'image_slug "registry.opensuse.org/opensuse/tumbleweed:latest@sha256:' + "b" * 64 + '"\n'
            'image_slug "alpine:edge@sha256:' + "c" * 64 + '"\n',
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert slug_out == [
        "ubuntu-26.04",
        "registry.opensuse.org-opensuse-tumbleweed-latest",
        "alpine-edge",
    ]
    deb_tag_out = subprocess.run(
        [
            "sh",
            "-ec",
            'for BASE in "ubuntu:26.04@sha256:' + "a" * 64 + '" "debian:testing@sha256:' + "b" * 64 + '"; do\n'
            "  BASE_NO_DIGEST=${BASE%@sha256:*}\n"
            '  echo "$(echo "${BASE_NO_DIGEST##*/}" | sed \'s/:\\([0-9]\\)/\\1/; s/[:@]/-/g\')"\n'
            "done\n",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert deb_tag_out == ["ubuntu26.04", "debian-testing"]

    # Build a synthetic copy of the managed files in tmp_path to exercise bump_repo and drift checks offline.
    to_copy = {
        pin_images.PINS_FILE.as_posix(),
        *pin_images.IMAGE_TARGETS,
        *pin_images.HARNESS_WORKFLOWS,
        *pin_images.RELEASE_GATE_SCRIPTS,
        *[p.relative_to(ROOT).as_posix() for p in (ROOT / ".github/workflows").glob("*.yml")],
        *[
            p.relative_to(ROOT).as_posix()
            for p in [*ROOT.glob("**/Dockerfile"), *ROOT.glob("**/*.Dockerfile"), *ROOT.glob("**/Containerfile")]
            if not p.relative_to(ROOT).as_posix().startswith(".")
        ],
    }
    for rel in sorted(to_copy):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst)
    assert pin_images.check_repo(tmp_path) == []

    # Deterministic bump rewrites image-pins.json and all target files cleanly while keeping
    # scheduled nightly rolling legs unpinned (Codex #389).
    import hashlib

    def fake_resolver(tag: str) -> dict[str, str]:
        d1 = "sha256:" + hashlib.sha256(f"index:{tag}".encode()).hexdigest()
        d2 = "sha256:" + hashlib.sha256(f"amd64:{tag}".encode()).hexdigest()
        return {"digest": d1, "amd64_digest": d2}

    pin_images.bump_repo(root=tmp_path, resolver=fake_resolver)
    assert pin_images.check_repo(tmp_path) == []
    # rpm-canary.yml intentionally stays unpinned fedora:latest, and scheduled nightly rolling
    # legs in source.yml / deb.yml / packages.yml remain unpinned after --bump.
    assert "build-aux/rpm/build.sh fedora:latest" in (tmp_path / ".github/workflows/rpm-canary.yml").read_text()
    bumped_source = (tmp_path / ".github/workflows/source.yml").read_text()
    assert '{"image":"fedora:latest"},{"image":"alpine:edge"}' in bumped_source
    assert ',{"image":"debian:testing","pytest-in":"Debian package / Build .deb (debian:testing)"}' in bumped_source
    assert (
        ',{"image":"registry.opensuse.org/opensuse/tumbleweed:latest",'
        '"pytest-in":"RPM and Arch packages / Build .rpm (registry.opensuse.org/opensuse/tumbleweed:latest)"}'
    ) in bumped_source
    assert ',{"image":"archlinux:latest","pytest-in":"RPM and Arch packages / Build Arch package"}' in bumped_source
    assert ', "debian:testing"]' in (tmp_path / ".github/workflows/deb.yml").read_text()
    bumped_packages = (tmp_path / ".github/workflows/packages.yml").read_text()
    assert '{"base":"registry.opensuse.org/opensuse/tumbleweed:latest","name":"opensuse"}' in bumped_packages
    assert "github.event_name == 'schedule' && 'archlinux:latest'" in bumped_packages

    # Unpinning a PR/main/release image, pinning a nightly schedule rolling leg, or admitting
    # schedule into release gates fails --check.
    bumped_pins = pin_images.load_pins(tmp_path)["images"]
    mutations = [
        (
            ".github/workflows/build.yml",
            lambda s: re.sub(r"container: ubuntu:26\.04@sha256:[0-9a-f]{64}", "container: ubuntu:26.04", s),
        ),
        (
            ".github/workflows/daily-test-release.yml",
            lambda s: re.sub(
                r"registry\.opensuse\.org/opensuse/tumbleweed:latest@sha256:[0-9a-f]{64}",
                "registry.opensuse.org/opensuse/tumbleweed:latest",
                s,
            ),
        ),
        (
            ".github/workflows/flatpak.yml",
            lambda s: re.sub(r"@sha256:[0-9a-f]{64}", ":gnome-50", s, count=1),
        ),
        (
            ".github/workflows/deb.yml",
            lambda s: re.sub(r"CASSINI_HARNESS_BASES: .*", "CASSINI_HARNESS_BASES: ubuntu:26.04@sha256:" + "f" * 64, s),
        ),
        (
            "tests/recording/Audio.Dockerfile",
            lambda s: re.sub(r"FROM ubuntu:26\.04@sha256:[0-9a-f]{64}", "FROM ubuntu:26.04", s),
        ),
        (
            "build-aux/deb/Dockerfile",
            lambda s: re.sub(r"ARG BASE=ubuntu:26\.04@sha256:[0-9a-f]{64}", "ARG BASE=ubuntu:26.04", s),
        ),
        (
            ".github/workflows/source.yml",
            lambda s: s.replace(
                '{"image":"fedora:latest"},{"image":"alpine:edge"}',
                f'{{"image":"fedora:latest@{bumped_pins["fedora:latest"]["digest"]}"}},{{"image":"alpine:edge"}}',
            ),
        ),
        (
            ".github/workflows/deb.yml",
            lambda s: s.replace(
                ', "debian:testing"]',
                f', "debian:testing@{bumped_pins["debian:testing"]["digest"]}"]',
            ),
        ),
        (
            ".github/workflows/packages.yml",
            lambda s: s.replace(
                '{"base":"registry.opensuse.org/opensuse/tumbleweed:latest","name":"opensuse"}',
                (
                    f'{{"base":"registry.opensuse.org/opensuse/tumbleweed:latest@'
                    f'{bumped_pins["registry.opensuse.org/opensuse/tumbleweed:latest"]["digest"]}","name":"opensuse"}}'
                ),
            ),
        ),
        (
            ".github/workflows/packages.yml",
            lambda s: s.replace(
                "github.event_name == 'schedule' && 'archlinux:latest'",
                f"github.event_name == 'schedule' && 'archlinux:latest@{bumped_pins['archlinux:latest']['digest']}'",
            ),
        ),
        (
            "build-aux/ci/green_main.py",
            lambda s: s.replace(
                'MAIN_EVENTS = ("push", "workflow_dispatch")',
                'MAIN_EVENTS = ("push", "schedule", "workflow_dispatch")',
            ),
        ),
        (
            "build-aux/ci/daily_test_release.py",
            lambda s: s.replace(
                'TRUSTED_CI_EVENTS = {"push", "workflow_dispatch"}',
                'TRUSTED_CI_EVENTS = {"push", "schedule", "workflow_dispatch"}',
            ),
        ),
    ]
    for rel, mutate in mutations:
        target = tmp_path / rel
        before = target.read_text()
        after = mutate(before)
        assert after != before, rel
        target.write_text(after)
        assert pin_images.check_repo(tmp_path), rel
        assert pin_images.main(["--check"], root=tmp_path) == 1, rel
        target.write_text(before)
        assert pin_images.check_repo(tmp_path) == [], rel


PERF_GATE_EXPR = (
    "${{ (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' || "
    "contains(github.event.pull_request.labels.*.name, 'full-ci')) && '1' || '' }}"
)


def test_nightly_and_full_ci_perf_gates_are_wired_and_forwarded():
    exact = (WORKFLOWS / "exact-transcript.yml").read_text()
    trigger = exact.split("\njobs:\n", 1)[0]
    assert re.search(r"^  schedule:\n    - cron: ", trigger, re.M)
    assert re.search(r"^  pull_request:\n    types: \[opened, synchronize, reopened, labeled\]$", trigger, re.M)
    assert re.search(r"^  workflow_dispatch:$", trigger, re.M)
    assert (
        "group: exact-transcript-${{ github.event_name == 'pull_request' && "
        "(github.event.action == 'labeled' && format('{0}-label-{1}', github.event.pull_request.number, github.run_id) "
        "|| github.event.pull_request.number) || github.run_id }}"
    ) in trigger
    assert (
        "cancel-in-progress: ${{ github.event_name == 'pull_request' && github.event.action != 'labeled' }}" in trigger
    )
    assert "tests/ui/exact-transcript.py exact-results/exact.png" in exact
    assert 'if [ "${CASSINI_PERF_GATE:-}" = "1" ]; then\n            CASSINI_TRANSCRIPT_RENDER=listview' in exact

    for name in (
        "source.yml",
        "deb.yml",
        "packages.yml",
        "meson-tests.yml",
        "recording.yml",
        "ux-shot.yml",
        "whistle.yml",
        "exact-transcript.yml",
    ):
        text = (WORKFLOWS / name).read_text()
        assert f"CASSINI_PERF_GATE: {PERF_GATE_EXPR}" in text, name

    container_sh = (ROOT / "build-aux/container.sh").read_text()
    assert '${CASSINI_PERF_GATE:+-e CASSINI_PERF_GATE="$CASSINI_PERF_GATE"}' in container_sh
    whistle_run = (ROOT / "tests/whistle/run.sh").read_text()
    assert '${CASSINI_PERF_GATE:+-e CASSINI_PERF_GATE="$CASSINI_PERF_GATE"}' in whistle_run

    recording = (WORKFLOWS / "recording.yml").read_text()
    assert "--test-skip-pattern" not in recording
    assert "- run: node --test browser-protocol/extension/tests/*.test.cjs" in recording
    capture_steps = re.split(r"\n(?=      - )", dict(jobs(WORKFLOWS / "recording.yml"))["capture"])[1:]
    host_pytest_steps = [
        step
        for step in capture_steps
        if "name: Python tests with private data on the temporary-file filesystem" in step
    ]
    assert len(host_pytest_steps) == 1
    host_pytest_if = re.search(r"^        if:\s*(.+)$", host_pytest_steps[0], re.M)
    assert host_pytest_if is not None
    rec_perf_expr = re.search(r"^  CASSINI_PERF_GATE:\s*(.+)$", recording, re.M).group(1)
    for evt, act, lbls, expected_gate in (
        ("pull_request", "opened", (), False),
        ("pull_request", "synchronize", ("bug", "ui-shots"), False),
        ("push", "", (), False),
        ("schedule", "", (), True),
        ("workflow_dispatch", "", (), True),
        ("pull_request", "synchronize", ("full-ci",), True),
        ("pull_request", "labeled", ("FULL-CI",), True),
    ):
        gate_val = str(_eval_actions_expr(rec_perf_expr, event=evt, action=act, labels=lbls))
        assert gate_val == ("1" if expected_gate else ""), (evt, act, lbls, gate_val)
        step_runs = bool(
            _eval_actions_expr(
                host_pytest_if.group(1),
                event=evt,
                action=act,
                labels=lbls,
                env={"CASSINI_PERF_GATE": gate_val},
            )
        )
        assert step_runs is expected_gate, (evt, act, lbls, step_runs)
    # Every capture scenario step in Recording/capture still runs unconditionally on PR and push.
    unconditional_capture_markers = (
        "node --test browser-protocol/extension/tests/*.test.cjs",
        "node --test tests/recording/signal.test.mjs",
        "tests/recording/native.py",
        "tests/recording/browser.mjs",
        "tests/recording/gtk.py",
        "tests/recording/stall.py",
    )
    for marker in unconditional_capture_markers:
        matching = [step for step in capture_steps if marker in step]
        assert len(matching) == 1 and not re.search(r"^        if:", matching[0], re.M), marker

    exact_ui = (ROOT / "tests/ui/exact-transcript.py").read_text()
    assert "\n        assert not page.list.ready and 0 < first_rows < len(model.turns)" in exact_ui
    assert "\n        assert len(load_samples) >= 6 and load_scrolls > 0" in exact_ui
    assert '\n        assert report["openFirstPaintCpuMs"] < report["openFullyLaidOutCpuMs"], report\n' in exact_ui
    assert (
        '\n        assert report["openFirstPaintFrameClockMs"] < report["openFullyLaidOutFrameClockMs"], report\n'
        in exact_ui
    )

    meeting_ui = (ROOT / "tests/ui/meeting.py").read_text()
    assert "CASSINI_PERF_GATE" not in meeting_ui
    assert '\n    assert 7000 <= advance <= 9000, ("2× clock advance", advance)\n' in meeting_ui

    browser_rec = (ROOT / "tests/recording/browser.mjs").read_text()
    assert "CASSINI_PERF_GATE" not in browser_rec
    assert "\n  assert.ok(Math.abs(mutePosition+original.startOffsetMs/1000-wallMutePosition)<.4," in browser_rec
    assert "\n  for(const measure of continuity) assert.ok(Math.abs(measure.error)<.25," in browser_rec


def _parse_workflow_dag(workflow: Path) -> dict[str, tuple[list[str], str | None]]:
    dag: dict[str, tuple[list[str], str | None]] = {}
    for name, block in jobs(workflow):
        header = re.split(r"\n    (?:steps|uses):", block, maxsplit=1)[0]
        if list_needs := re.search(r"^    needs:\s*\[([^\]]*)\]\s*$", header, re.M):
            needs = [item.strip() for item in list_needs.group(1).split(",") if item.strip()]
        elif single_need := re.search(r"^    needs:\s*([A-Za-z0-9_-]+)\s*$", header, re.M):
            needs = [single_need.group(1)]
        else:
            needs = []
        cond_match = re.search(r"^    if:\s*(.+)$", header, re.M)
        cond = cond_match.group(1).strip().removeprefix("${{").removesuffix("}}").strip() if cond_match else None
        dag[name] = (needs, cond)
    return dag


def _eval_actions_expr(
    expr: str,
    *,
    event: str = "pull_request",
    action: str = "synchronize",
    label: str = "",
    labels: tuple[str, ...] = (),
    pr_number: int = 392,
    run_id: int = 100,
    needs_results: dict[str, str] | None = None,
    changes_outputs: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    ancestors_ok: bool = True,
    failed: bool = False,
    cancelled: bool = False,
):
    py = expr.removeprefix("${{").removesuffix("}}").strip()
    for job_id in sorted((needs_results or {}), key=len, reverse=True):
        if "-" in job_id:
            py = py.replace(f"needs.{job_id}.", f"needs.{job_id.replace('-', '_')}.")
    py = re.sub(r"([A-Za-z0-9_.]+)\.\*\.([A-Za-z0-9_]+)", r"_star(\1, '\2')", py)
    py = py.replace("always()", "True")
    py = py.replace("cancelled()", str(cancelled))
    py = py.replace("failure()", str(failed))
    py = py.replace("success()", str(ancestors_ok and not failed and not cancelled))
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    effective_labels = labels or ((label,) if label else ())
    event_ns = (
        SimpleNamespace(
            action=_CIString(action),
            label=SimpleNamespace(name=_CIString(label)),
            pull_request=SimpleNamespace(
                number=pr_number,
                labels=[SimpleNamespace(name=_CIString(lbl)) for lbl in effective_labels],
            ),
        )
        if event == "pull_request"
        else _NULL
    )
    needs_dict: dict[str, object] = {}
    for k, v in (needs_results or {}).items():
        outputs_ns = (
            SimpleNamespace(**{ok: _CIString(ov) for ok, ov in (changes_outputs or {}).items()})
            if k == "changes"
            else _NULL
        )
        needs_dict[k.replace("-", "_")] = SimpleNamespace(result=_CIString(v), outputs=outputs_ns)
    env_ns = SimpleNamespace(**{k: _CIString(v) for k, v in (env or {}).items()})

    def _star(seq, attr: str):
        return [getattr(item, attr) for item in seq] if isinstance(seq, list) else []

    def _contains(search, item) -> bool:
        if isinstance(search, list):
            return any(_CIString(str(x)) == str(item) for x in search)
        if isinstance(search, str):
            return str(item).casefold() in search.casefold()
        return False

    ctx = {
        "github": SimpleNamespace(event_name=_CIString(event), event=event_ns, run_id=run_id),
        "needs": SimpleNamespace(**needs_dict),
        "env": env_ns,
        "True": True,
        "False": False,
        "format": lambda template, *args: template.format(*args),
        "contains": _contains,
        "_star": _star,
    }
    return eval(py, {"__builtins__": {}}, ctx)


def _simulate_dag(
    dag: dict[str, tuple[list[str], str | None]],
    *,
    changes_outputs: dict[str, str] | None = None,
    changes_failed: bool = False,
    **kwargs,
) -> dict[str, str]:
    status_fns = ("always()", "cancelled()", "failure()", "success()")
    results: dict[str, str] = {}
    remaining = list(dag)

    def ancestors(job: str) -> set[str]:
        seen: set[str] = set()
        stack = list(dag[job][0])
        while stack:
            dep = stack.pop()
            if dep not in seen:
                seen.add(dep)
                stack.extend(dag[dep][0])
        return seen

    while remaining:
        for job in list(remaining):
            needs, cond = dag[job]
            if any(dep not in results for dep in needs):
                continue
            anc_ok = all(results[anc] == "success" for anc in ancestors(job))
            if cond is None:
                run = anc_ok
            else:
                effective = cond if any(fn in cond for fn in status_fns) else f"success() && ({cond})"
                run = bool(
                    _eval_actions_expr(
                        effective,
                        needs_results={dep: results[dep] for dep in needs},
                        changes_outputs=changes_outputs if results.get("changes") == "success" else {},
                        ancestors_ok=anc_ok,
                        **kwargs,
                    )
                )
            if not run:
                results[job] = "skipped"
            elif job == "changes" and changes_failed:
                results[job] = "failure"
            else:
                results[job] = "success"
            remaining.remove(job)
    return results


def test_labeled_pr_isolation_and_full_dag_across_events():
    for wf_name, prefix in (("ci.yml", "ci"), ("exact-transcript.yml", "exact-transcript")):
        header = (WORKFLOWS / wf_name).read_text().split("\njobs:\n", 1)[0]
        group_tpl = re.search(r"^  group: (.+)$", header, re.M).group(1)
        cancel_expr = re.search(r"^  cancel-in-progress: (.+)$", header, re.M).group(1)

        def render_group(tpl=group_tpl, **kw):
            return re.sub(r"\$\{\{.*?\}\}", lambda m: str(_eval_actions_expr(m.group(0), **kw)), tpl)

        assert render_group(event="pull_request", action="opened", pr_number=42, run_id=10) == f"{prefix}-42"
        assert render_group(event="pull_request", action="synchronize", pr_number=42, run_id=11) == f"{prefix}-42"
        assert render_group(event="pull_request", action="labeled", pr_number=42, run_id=12) == f"{prefix}-42-label-12"
        assert render_group(event="pull_request", action="labeled", pr_number=42, run_id=13) == f"{prefix}-42-label-13"
        assert render_group(event="push", action="", run_id=99) == f"{prefix}-99"
        assert render_group(event="schedule", action="", run_id=100) == f"{prefix}-100"
        assert bool(_eval_actions_expr(cancel_expr, event="pull_request", action="opened")) is True
        assert bool(_eval_actions_expr(cancel_expr, event="pull_request", action="synchronize")) is True
        assert bool(_eval_actions_expr(cancel_expr, event="pull_request", action="labeled")) is False
        assert bool(_eval_actions_expr(cancel_expr, event="push", action="")) is False
        assert bool(_eval_actions_expr(cancel_expr, event="schedule", action="")) is False

    ci_dag = _parse_workflow_dag(WORKFLOWS / "ci.yml")
    exact_dag = _parse_workflow_dag(WORKFLOWS / "exact-transcript.yml")
    release_dag = _parse_workflow_dag(WORKFLOWS / "release.yml")

    all_areas_true = dict.fromkeys(
        (
            "source",
            "packages",
            "recording",
            "boards",
            "library",
            "offline",
            "deb_testing",
            "rpm_opensuse",
            "arch",
            "flatpak",
            "extra_distros",
            "full_ci",
        ),
        "true",
    )
    pr_code_outputs = {
        **all_areas_true,
        "deb_testing": "false",
        "rpm_opensuse": "false",
        "arch": "false",
        "flatpak": "false",
        "extra_distros": "false",
        "full_ci": "false",
    }
    pr_docs_outputs = {
        **pr_code_outputs,
        "recording": "false",
        "boards": "false",
        "library": "false",
        "offline": "false",
    }
    all_ci_jobs = set(ci_dag)

    # Unlabeled PR (code changes vs docs-only):
    for action in ("opened", "synchronize", "reopened"):
        res_code = _simulate_dag(ci_dag, event="pull_request", action=action, changes_outputs=pr_code_outputs)
        assert {j for j, s in res_code.items() if s == "success"} == all_ci_jobs - {"flatpak"}
        res_docs = _simulate_dag(ci_dag, event="pull_request", action=action, changes_outputs=pr_docs_outputs)
        assert {j for j, s in res_docs.items() if s == "success"} == {
            "changes",
            "build",
            "meson-tests",
            "source",
            "deb",
            "rpm-arch",
            "pytest-coverage",
            "desktop",
        }
        assert _simulate_dag(exact_dag, event="pull_request", action=action) == {"native-gtk": "success"}

    # Labeled PR with full-ci (case-insensitive): runs full matrix + native-gtk:
    for lbl in ("full-ci", "FULL-CI"):
        res_full = _simulate_dag(
            ci_dag, event="pull_request", action="labeled", label=lbl, changes_outputs=all_areas_true
        )
        assert {j for j, s in res_full.items() if s == "success"} == all_ci_jobs
        assert _simulate_dag(exact_dag, event="pull_request", action="labeled", label=lbl) == {"native-gtk": "success"}

    # Unrelated label on PR: skips every job cleanly in both workflows:
    for unrelated in ("bug", "documentation", "dependencies", "enhancement"):
        res_skip = _simulate_dag(
            ci_dag, event="pull_request", action="labeled", label=unrelated, changes_outputs=all_areas_true
        )
        assert {j for j, s in res_skip.items() if s == "success"} == set()
        assert _simulate_dag(exact_dag, event="pull_request", action="labeled", label=unrelated) == {
            "native-gtk": "skipped"
        }

    # Fail-open invariant when changes fails on a PR:
    res_fail_open = _simulate_dag(ci_dag, event="pull_request", action="synchronize", changes_failed=True)
    assert {j for j, s in res_fail_open.items() if s == "success"} == all_ci_jobs - {"changes"}

    # Push to main, schedule, workflow_dispatch, and release tag push:
    res_main = _simulate_dag(ci_dag, event="push", action="", changes_outputs={**all_areas_true, "full_ci": "false"})
    assert {j for j, s in res_main.items() if s == "success"} == all_ci_jobs
    for evt in ("schedule", "workflow_dispatch"):
        res_sched = _simulate_dag(ci_dag, event=evt, action="", changes_outputs=all_areas_true)
        assert {j for j, s in res_sched.items() if s == "success"} == all_ci_jobs
        assert _simulate_dag(exact_dag, event=evt, action="") == {"native-gtk": "success"}
    res_tag = _simulate_dag(release_dag, event="push", action="")
    assert {j for j, s in res_tag.items() if s == "success"} == {"main-inputs", "deb", "packages", "publish"}

    # Prove _simulate_dag catches the skipped-ancestor bug class on implicit-success jobs (`release.yml:publish`).
    regressed_release = dict(release_dag)
    regressed_release["label-gate"] = ([], "github.event_name == 'pull_request'")
    regressed_release["main-inputs"] = (["label-gate"], "always()")
    regressed_release["deb"] = (["main-inputs"], "always() && needs.main-inputs.result == 'success'")
    regressed_release["packages"] = (["main-inputs"], "always() && needs.main-inputs.result == 'success'")
    assert _simulate_dag(regressed_release, event="push", action="")["publish"] == "skipped"


def test_prepare_tumbleweed_origin_and_cdn_fallback(tmp_path):
    script = ROOT / "build-aux/ci/prepare_tumbleweed.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "zypper.log"
    fake_zypper = bin_dir / "zypper"
    fake_zypper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'repos_state=$(grep "^baseurl=" "$CASSINI_ZYPP_REPOS_DIR"/oss.repo)\n'
        'printf "%s|%s\\n" "$*" "$repos_state" >> "$ZYPPER_LOG"\n'
        'case "$*|$repos_state" in\n'
        "  *refresh*|*dist-upgrade*)\n"
        "    for bad in ${FAIL_HOSTS:-}; do\n"
        '      case "$repos_state" in *"https://${bad}/"*) exit 4 ;; esac\n'
        "    done\n"
        "    ;;\n"
        "esac\n"
    )
    fake_zypper.chmod(0o755)

    def make_env(os_id: str, fail_hosts: str = ""):
        case_dir = tmp_path / f"case_{os_id}_{fail_hosts.replace(' ', '_') or 'ok'}"
        repos = case_dir / "repos.d"
        repos.mkdir(parents=True, exist_ok=True)
        os_release = case_dir / "os-release"
        os_release.write_text(f"ID={os_id}\n")
        (repos / "oss.repo").write_text(
            "[repo-oss]\nenabled=1\nbaseurl=http://download.opensuse.org/tumbleweed/repo/oss/\ngpgcheck=1\n"
        )
        (repos / "openh264.repo").write_text(
            "[repo-openh264]\nenabled=1\nbaseurl=http://codecs.opensuse.org/openh264/openSUSE_Tumbleweed\ngpgcheck=1\n"
        )
        log_file.write_text("")
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
            CASSINI_OS_RELEASE=str(os_release),
            CASSINI_ZYPP_REPOS_DIR=str(repos),
            ZYPPER_LOG=str(log_file),
            FAIL_HOSTS=fail_hosts,
        )
        return repos, env

    # 1. Non-Tumbleweed container exits 0 without touching repo files or calling zypper.
    repos, env = make_env("fedora")
    res = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert log_file.read_text() == ""
    assert "http://download.opensuse.org/" in (repos / "oss.repo").read_text()

    # 2. Healthy origin (downloadcontent.opensuse.org) succeeds without fallback and preserves custom repos/gpgcheck.
    repos, env = make_env("opensuse-tumbleweed")
    res = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "baseurl=https://downloadcontent.opensuse.org/tumbleweed/repo/oss/" in (repos / "oss.repo").read_text()
    assert "gpgcheck=1" in (repos / "oss.repo").read_text()
    assert "baseurl=http://codecs.opensuse.org/openh264/openSUSE_Tumbleweed" in (repos / "openh264.repo").read_text()
    assert log_file.read_text().splitlines() == [
        "--non-interactive refresh|baseurl=https://downloadcontent.opensuse.org/tumbleweed/repo/oss/",
        "--non-interactive dist-upgrade --no-recommends --no-allow-vendor-change|baseurl=https://downloadcontent.opensuse.org/tumbleweed/repo/oss/",
    ]

    # 3. Origin 503 (exit 4 on downloadcontent.opensuse.org) cleans metadata and falls back to cdn.opensuse.org,
    #    leaving oss.repo pointed at cdn.opensuse.org for subsequent zypper install steps.
    repos, env = make_env("opensuse-tumbleweed", fail_hosts="downloadcontent.opensuse.org")
    res = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "downloadcontent.opensuse.org failed (exit 4)" in res.stderr
    assert "baseurl=https://cdn.opensuse.org/tumbleweed/repo/oss/" in (repos / "oss.repo").read_text()
    assert "baseurl=http://codecs.opensuse.org/openh264/openSUSE_Tumbleweed" in (repos / "openh264.repo").read_text()
    assert log_file.read_text().splitlines() == [
        "--non-interactive refresh|baseurl=https://downloadcontent.opensuse.org/tumbleweed/repo/oss/",
        "--non-interactive clean --all|baseurl=https://downloadcontent.opensuse.org/tumbleweed/repo/oss/",
        "--non-interactive refresh|baseurl=https://cdn.opensuse.org/tumbleweed/repo/oss/",
        "--non-interactive dist-upgrade --no-recommends --no-allow-vendor-change|baseurl=https://cdn.opensuse.org/tumbleweed/repo/oss/",
    ]

    # 4. Both origin and CDN failing falls back to download.opensuse.org; all three failing propagates zypper exit 4.
    repos, env = make_env("opensuse-tumbleweed", fail_hosts="downloadcontent.opensuse.org cdn.opensuse.org")
    res = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "baseurl=https://download.opensuse.org/tumbleweed/repo/oss/" in (repos / "oss.repo").read_text()

    repos, env = make_env(
        "opensuse-tumbleweed",
        fail_hosts="downloadcontent.opensuse.org cdn.opensuse.org download.opensuse.org",
    )
    res = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert res.returncode == 4


def test_pr_diagnostic_evidence_artifacts_upload_only_on_failure():
    pr_failure_if = "${{ failure() || (github.event_name != 'pull_request' && always()) }}"
    ubuntu_pr_failure_if = (
        "${{ (failure() || (github.event_name != 'pull_request' && always()))"
        " && startsWith(matrix.base, 'ubuntu:26.04@sha256:') }}"
    )
    expected_diagnostic = {
        ("recording.yml", "recording-evidence"): pr_failure_if,
        ("recording.yml", "live-folder-evidence"): pr_failure_if,
        ("background-apps.yml", "background-apps-desktop"): pr_failure_if,
        ("whistle.yml", "voices-linux-x86_64"): pr_failure_if,
        ("whistle.yml", "diarization-linux-x86_64"): pr_failure_if,
        ("whistle.yml", "parakeet-linux-x86_64"): pr_failure_if,
        ("whistle.yml", "whistle-linux-x86_64"): pr_failure_if,
        ("exact-transcript.yml", "exact-transcript-measurements"): pr_failure_if,
        ("ux-shot.yml", "ux-boards"): pr_failure_if,
        ("ux-library.yml", "gnome-ux-library-vectors"): pr_failure_if,
        ("flatpak.yml", "flatpak-bridge-evidence"): pr_failure_if,
        ("meson-tests.yml", "meson-logs-minimal"): pr_failure_if,
        ("deb.yml", "meson-logs-deb-${{ strategy.job-index }}"): pr_failure_if,
        ("deb.yml", "installed-build-refresh"): ubuntu_pr_failure_if,
        ("deb.yml", "browser-startup-installed"): ubuntu_pr_failure_if,
        ("packages.yml", "meson-logs-rpm-${{ matrix.name }}"): pr_failure_if,
        ("packages.yml", "meson-logs-arch"): pr_failure_if,
        ("source.yml", "meson-logs-distro-${{ strategy.job-index }}"): pr_failure_if,
    }
    downstream_unfiltered_by_failure = {
        ("ci.yml", "changed-areas"),
        ("build.yml", "ci-core"),
        ("build.yml", "ci-boards"),
        ("build.yml", "ci-offline"),
        ("deb.yml", "pytest-ran-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}"),
        ("deb.yml", "deb-${{ strategy.job-index }}"),
        ("packages.yml", "pytest-ran-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}"),
        ("packages.yml", "rpm-${{ matrix.name }}"),
        ("packages.yml", "arch"),
        (
            "source.yml",
            "pytest-${{ steps.pytest.outputs.kind }}-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}",
        ),
        ("flatpak.yml", "flatpak-stable"),
        ("ux-shot.yml", "pr-shots"),
    }

    seen_uploads: dict[tuple[str, str], list[str]] = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        if "\njobs:\n" not in text:
            continue
        for _, block in jobs(workflow):
            uncommented = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
            for step in re.split(r"\n(?=      - )", uncommented):
                if "actions/upload-artifact@" not in step:
                    continue
                name_match = re.search(r"^          name: *(.+)$", step, re.M)
                assert name_match, (workflow.name, step)
                if_match = re.search(r"^        if: *(.+)$", step, re.M)
                seen_uploads.setdefault((workflow.name, name_match.group(1).strip()), []).append(
                    if_match.group(1).strip() if if_match else ""
                )

    for key, expected_if in expected_diagnostic.items():
        assert seen_uploads.get(key) == [expected_if], (key, seen_uploads.get(key))

    for key in downstream_unfiltered_by_failure:
        assert key in seen_uploads, key
        assert all("failure()" not in cond for cond in seen_uploads[key]), (key, seen_uploads[key])

    # Evaluate the GitHub Actions status condition across event/failure/base combinations.
    def eval_upload_if(cond: str, *, event_name: str, failed: bool, base: str = "ubuntu:26.04@sha256:00") -> bool:
        expr = cond.removeprefix("${{").removesuffix("}}").strip()
        py_expr = (
            expr.replace("&&", " and ")
            .replace("||", " or ")
            .replace("!=", " != ")
            .replace("github.event_name", "event_name")
            .replace("matrix.base", "base")
        )
        ns = {
            "event_name": event_name,
            "base": base,
            "failure": lambda: failed,
            "always": lambda: True,
            "startsWith": lambda s, prefix: s.startswith(prefix),
        }
        return bool(eval(py_expr, {"__builtins__": {}}, ns))  # noqa: S307

    for cond in (pr_failure_if, ubuntu_pr_failure_if):
        assert eval_upload_if(cond, event_name="pull_request", failed=False) is False
        assert eval_upload_if(cond, event_name="pull_request", failed=True) is True
        for event_name in ("push", "schedule", "workflow_dispatch"):
            assert eval_upload_if(cond, event_name=event_name, failed=False) is True
            assert eval_upload_if(cond, event_name=event_name, failed=True) is True
    assert (
        eval_upload_if(ubuntu_pr_failure_if, event_name="push", failed=True, base="debian:testing@sha256:00") is False
    )

    # ux-boards must sit after Compare UI screenshots against main baseline so a
    # pr-shots.py diff failure on a PR still triggers the ux-boards diagnostic upload.
    ux_shot = (WORKFLOWS / "ux-shot.yml").read_text()
    assert ux_shot.index("- name: Compare UI screenshots against main baseline") < ux_shot.index("name: pr-shots")
    assert ux_shot.index("name: pr-shots") < ux_shot.index("name: ux-boards")
