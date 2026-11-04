# SPDX-License-Identifier: GPL-3.0-or-later
"""G13/G14: every job and every local test run has a time budget, and CI runs on pushes to main only."""

import os
import re
import shutil
import subprocess
from pathlib import Path

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


def test_package_builds_run_on_the_pinned_runner_image():
    # G16: the package builds run on the image every other job pins, not on
    # whatever ubuntu-latest points to. release.yml's publish job only uploads.
    for name in ("deb.yml", "packages.yml", "rpm-canary.yml"):
        images = re.findall(r"^    runs-on: (\S+)", (WORKFLOWS / name).read_text(), re.M)
        assert images and set(images) == {"ubuntu-26.04"}, (name, images)


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


HARNESS_IMAGES = {
    "desktop": "tests/desktop/Dockerfile",
    "build-refresh": "tests/build-refresh/Dockerfile",
    "browser-setup": "tests/browser-setup/Dockerfile",
    "whistle": "tests/whistle/Containerfile",
}


def steps(workflow):
    """Each step of a workflow, as its own text."""
    return re.split(r"\n(?=      - )", workflow.read_text())[1:]


def test_caches_are_saved_from_main_only():
    # D66: a pull request restores caches and never saves one. The harness
    # images are only ever restored, then saved by an explicit main-only step.
    main_only = "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    saves, restores = [], []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for step in steps(workflow):
            if "uses: actions/cache/save@" in step or "harness-cache.sh save" in step:
                saves.append(workflow.name)
                assert re.search(r"^        if: .*" + re.escape(main_only), step, re.M), (workflow.name, step)
            if "harness-cache" in step and "uses: actions/cache@" in step:
                raise AssertionError(f"{workflow.name}: harness images through actions/cache, which saves on PRs")
            if "uses: actions/cache/restore@" in step and "steps.harness.outputs.key" in step:
                restores.append(workflow.name)
    assert sorted(restores) == ["background-apps.yml", "deb.yml", "whistle.yml"]
    assert sorted(saves) == sorted(restores * 2)


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
        assert separator and "ARG HARNESS=harness\n" in harness and re.search(r"^FROM \S+ AS harness$", harness, re.M)
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
            current = [line for line in builds if '--build-arg HARNESS="$harness"' in line]
            assert current and all("--no-cache" in line and "--pull" not in line for line in current), script
            # Nothing runs in the cached stage itself.
            runs = [line for line in lines if re.search(r"(^|\s)docker run\s", line)]
            assert runs and not any(re.search(r'"\$harness"\s', line) for line in runs), script


def harness_tag(root, name, epoch="2026-W45", **env):
    return subprocess.run(
        ["bash", str(root / "tests/harness-image.sh"), "tag", name],
        env={"PATH": os.environ["PATH"], "CASSINI_HARNESS_EPOCH": epoch, **env},
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
    assert 'test "$MESON_TESTS" = success ||' in ci["pytest-coverage"]
    (tests,) = dict(jobs(WORKFLOWS / "meson-tests.yml")).values()
    products = dict(jobs(WORKFLOWS / "build.yml"))["products"]
    for line in ("meson test -C _build-core --print-errorlogs", "meson compile -C _build-core cassini-talk-pot"):
        assert line in tests and line not in products
    assert "container: ubuntu:26.04" in tests and "if:" not in tests
    # The same minimal environment as the products' build.
    install = re.compile(r"- name: Install build tools \(minimal source-validation environment\)\n(?:        .*\n)+")
    assert install.search(tests)[0] == install.search(products)[0]
