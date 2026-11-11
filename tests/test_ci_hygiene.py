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
            assert base == "ubuntu:26.04" or ("$" in base and ENTRYPOINT_CHECK in script.read_text()), (name, base)
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
        ("ci.yml", "changes"),
        ("ci.yml", "pytest-coverage"),
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
        "uses: ./.github/workflows/meson-tests.yml",
    ]
    for name in ("recording", "boards", "offline"):
        assert re.search(r"^    needs: \[changes, build\]$", ci[name], re.M) and "meson-tests" not in ci[name], name
    assert "needs: [source, deb, rpm-arch, meson-tests]" in ci["pytest-coverage"]
    for var in ("SOURCE", "DEB", "RPM_ARCH", "MESON_TESTS"):
        assert f'test "${var}" = success ||' in ci["pytest-coverage"]
    (tests,) = dict(jobs(WORKFLOWS / "meson-tests.yml")).values()
    products = dict(jobs(WORKFLOWS / "build.yml"))["products"]
    for line in ("meson test -C _build-core --print-errorlogs", "meson compile -C _build-core cassini-talk-pot"):
        assert line in tests and line not in products
    assert "container: ubuntu:26.04" in tests and not re.search(r"^    if:", tests, re.M)
    for step in re.split(r"^      - ", tests, flags=re.M)[1:]:
        if "if:" in step:
            assert step.startswith("uses: actions/upload-artifact@v7\n") and "if: always()" in step
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
