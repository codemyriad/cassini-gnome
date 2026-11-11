# SPDX-License-Identifier: GPL-3.0-or-later
"""G11 (D20): a Source distro job skips pytest only where a required package
job runs the complete suite in the same image.

`problems` reads the files that decide it; every mutation below must make it
fail. At run time build-aux/ci/check_pytest_ran.py proves from the package
job's Meson logs that every test file ran (tests/test_check_pytest_ran.py),
and CI's Pytest coverage gate requires that proof for every skipping image.
"""

import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/source.yml",
    ".github/workflows/deb.yml",
    ".github/workflows/packages.yml",
    "debian/control",
    "debian/rules",
    "build-aux/rpm/cassini-talk.spec",
    "build-aux/arch/PKGBUILD",
    "build-aux/deb/build.sh",
    "build-aux/rpm/build.sh",
    "build-aux/arch/build.sh",
    "build-aux/check-distros.sh",
    "build-aux/deb/Dockerfile",
    "build-aux/ci/pr-paths.json",
    "pyproject.toml",
    "tests/conftest.py",
)
# Read when present: debhelper deletes the files debian/clean lists and
# applies debian/patches; pytest reads its config from the first of these.
OPTIONAL = ("debian/clean", "debian/patches/series", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml")
OPTIONAL += ("tox.ini", "setup.cfg", "conftest.py")
OS_RELEASE = "cp /etc/os-release /logs/os-release  # G11: the image that ran the tests"
# The Source images that keep their pytest suite: no package job uses them.
KEEPS_PYTEST = {"fedora:latest", "alpine:edge"}

RULES_CHECK = [
    "override_dh_auto_test:",
    "ifeq (,$(filter nocheck,$(DEB_BUILD_OPTIONS)))",
    "\tCASSINI_TEST_REQUIRE_GTK=1 meson test -C obj-$(DEB_HOST_GNU_TYPE) --num-processes=2 --print-errorlogs",
    "endif",
]
SPEC_CHECK = ["export CASSINI_TEST_REQUIRE_GTK=1", "%meson_test --num-processes=2"]
ARCH_CHECK = ["check() {", "CASSINI_TEST_REQUIRE_GTK=1 meson test -C build --num-processes=2 --print-errorlogs", "}"]
CHECK_STEP = """
      - name: Require the whole pytest suite to have run and passed
        id: pytest
        run: |
          python3 build-aux/ci/check_pytest_ran.py "$CASSINI_TEST_LOGS" --export "$RUNNER_TEMP/pytest" \\
            --github-output "$GITHUB_OUTPUT"
      - uses: actions/upload-artifact@v7
        with:
          name: pytest-ran-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}
          path: ${{ runner.temp }}/pytest/
          if-no-files-found: error
"""
SKIP_ENV = """
    env:
      CASSINI_SKIP_PYTEST: ${{ matrix.pytest-in && '1' || '' }}
    steps:
"""
SOURCE_STEP = """
      - run: build-aux/check-distros.sh "${{ matrix.image }}"
      # G11: from this job's own Meson logs, the node ids its pytest ran
      # (fedora:latest's are the reference every package job must cover),
      # or, whatever made it skip, a pytest-skipped marker that CI's Pytest
      # coverage gate requires a package job's proof for. Only run after
      # check-distros.sh succeeds so a failure before pytest never uploads a
      # skip marker; suffix run_attempt so re-runs supersede stale markers.
      - name: Record which pytest tests ran here
        id: pytest
        run: |
          . build-aux/container.sh
          python3 build-aux/ci/check_pytest_ran.py --source "$CASSINI_TEST_LOGS" \\
            --install-outcome "$CASSINI_INSTALL_OUTCOME" --slug "$(image_slug "${{ matrix.image }}")" \\
            --export "$RUNNER_TEMP/pytest" --github-output "$GITHUB_OUTPUT"
      - uses: actions/upload-artifact@v7
        with:
          name: pytest-${{ steps.pytest.outputs.kind }}-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}
          path: ${{ runner.temp }}/pytest/
          if-no-files-found: error
"""
GATE = """
  pytest-coverage:
    name: Pytest coverage
    needs: [source, deb, rpm-arch, meson-tests]
    if: ${{ !cancelled() }}
    runs-on: ubuntu-26.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v7
      - uses: actions/download-artifact@v8
        with:
          pattern: pytest-*
          path: ${{ runner.temp }}/pytest
      - name: Every skipped pytest suite ran in full in a package job of the same release
        run: python3 build-aux/ci/check_pytest_ran.py --gate "$RUNNER_TEMP/pytest"
      - name: The Meson tests ran and passed
        if: ${{ !cancelled() }}
        env:
          MESON_TESTS: ${{ needs.meson-tests.result }}
        run: test "$MESON_TESTS" = success || { echo "::error::Meson tests $MESON_TESTS, not success"; exit 1; }
"""
PYTEST_OPTIONS = {"faulthandler_timeout": 30, "pythonpath": ["src"], "testpaths": ["tests"]}
BUILDS = {
    "deb": ('build-aux/deb/build.sh "${{ matrix.base }}"', "build-aux/deb/build.sh", "dpkg-buildpackage -us -uc -b"),
    "rpm": (
        'build-aux/rpm/build.sh "${{ matrix.base }}"',
        "build-aux/rpm/build.sh",
        'rpmbuild -ba --define "_topdir $top" --define "dist $dist" "$@" "$spec"',
    ),
    "arch": ("build-aux/arch/build.sh", "build-aux/arch/build.sh", 'su builder -c "makepkg -s --noconfirm"'),
}
# What the suite needs to run every test, GTK application tests included.
DEB_DEPS = (
    "python3-pytest",
    "python3-jsonschema",
    "python3-rfc3339-validator",
    "python3-gi",
    "python3-numpy",
    "gir1.2-gstreamer-1.0",
    "gir1.2-gtk-4.0",
    "gir1.2-adw-1",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "libglib2.0-dev-bin",
)
RPM_DEPS = (
    "python3-pytest",
    "python3-jsonschema",
    "python3-rfc3339-validator",
    "python3-gobject",
    "python3dist(numpy)",
    "glib2-devel",
)
SUSE_DEPS = (
    "typelib-1_0-Gst-1_0",
    "typelib-1_0-Gtk-4_0",
    "typelib-1_0-Adw-1",
    "gstreamer-plugins-base",
    "gstreamer-plugins-good",
)
FEDORA_DEPS = (
    "gobject-introspection",
    "gstreamer1",
    "gstreamer1-plugins-base",
    "gstreamer1-plugins-good",
    "gtk4",
    "libadwaita",
)
ARCH_DEPS = (
    "python-gobject",
    "gtk4",
    "libadwaita",
    "gstreamer",
    "gst-plugins-base",
    "gst-plugins-good",
    "python-numpy",
    "python-pytest",
    "python-jsonschema",
    "python-rfc3339-validator",
)


def read():
    files = {name: (ROOT / name).read_text() for name in FILES}
    files.update({name: (ROOT / name).read_text() if (ROOT / name).exists() else "" for name in OPTIONAL})
    return files


def jobs(text):
    body = text.split("\njobs:\n", 1)[1]
    return {
        match.group(1): block
        for block in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + body)
        if (match := re.match(r"\s*([A-Za-z0-9_-]+):\n", block))
    }


def code(lines):
    """Lines without comments and blank lines (shell and make)."""
    return [line.rstrip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def section(text, start, stop):
    match = re.search(start, text, re.M)
    if match is None:
        return None
    rest = text[match.start() :]
    end = re.search(stop, rest[1:], re.M)
    return rest[: end.start() + 1] if end else rest


def deb_build_depends(control):
    block = re.search(r"^Build-Depends:(.*?)^(?=\S)", control, re.M | re.S)[1]
    entries = {}
    for entry in (e.strip() for e in block.split(",")):
        if match := re.fullmatch(r"([a-z0-9.+-]+(?::any)?)(\s*\([^)]*\))?(\s*<[^>]*>)?", entry):
            entries[match[1]] = (match[3] or "").strip()
    return entries


def spec_requires(spec):
    """{"common"|"openSUSE"|"Fedora": BuildRequires} by where they sit: at
    top level, or directly in the one suse_version %if or its %else; a
    BuildRequires inside any other conditional counts nowhere."""
    found, stack = {"common": set(), "openSUSE": set(), "Fedora": set()}, []
    for line in spec.splitlines():
        if line.startswith("%if"):
            stack.append([line.strip(), False])
        elif line.startswith("%else") and stack:
            stack[-1][1] = True
        elif line.startswith("%endif") and stack:
            stack.pop()
        elif match := re.match(r"BuildRequires:\s+(\S+)", line):
            if not stack:
                found["common"].add(match[1])
            elif len(stack) == 1 and stack[0][0] == "%if 0%{?suse_version}":
                found["Fedora" if stack[0][1] else "openSUSE"].add(match[1])
    return found


def arch_array(pkgbuild, name):
    body = re.search(rf"^{name}=\((.*?)\)", re.sub(r"#[^\n]*", "", pkgbuild), re.M | re.S)[1]
    words = body.split()
    return {re.split(r"[<>=]", word.strip("'\""))[0] for word in words}


def problems(files):
    found = []
    source = jobs(files[".github/workflows/source.yml"])["distros"]
    if re.search(r"^    (?:if|needs):", source, re.M) or "exclude:" in source or "continue-on-error" in source:
        found.append("Source job or matrix can omit coverage proofs")
    entries = re.findall(r'^          - image: (\S+)\n(?:            pytest-in: "([^"]+)"\n)?', source, re.M)
    skipped = {image: job for image, job in entries if job}
    if {image for image, job in entries if not job} != KEEPS_PYTEST:
        found.append(f"Source keeps pytest on {sorted(i for i, j in entries if not j)}, not {sorted(KEEPS_PYTEST)}")
    if len([line for line in code(source.splitlines()) if "pytest-in:" in line]) != len(skipped):
        found.append("Source has a pytest-in this guard cannot read")
    if source.count("CASSINI_SKIP_PYTEST:") != 1 or SKIP_ENV not in source:
        found.append("Source skips pytest other than through the job's pytest-in")
    if (
        'echo "CASSINI_TEST_LOGS=$RUNNER_TEMP/meson-logs" >> "$GITHUB_ENV"' not in source
        or 'echo "CASSINI_INSTALL_OUTCOME=$RUNNER_TEMP/install-outcome" >> "$GITHUB_ENV"' not in source
    ):
        found.append("Source does not isolate install outcome from test-writable logs")
    if not re.search(re.escape(SOURCE_STEP) + r"(?!        )", source):
        found.append("Source does not record from its own logs whether its pytest ran")
    distros = files["build-aux/check-distros.sh"]
    if distros.count("CASSINI_SKIP_PYTEST") != 4 or 'if [ "${CASSINI_SKIP_PYTEST:-}" = 1 ]; then' not in distros:
        found.append("check-distros.sh decides on its own whether to skip pytest")
    for script in (
        "build-aux/check-distros.sh",
        "build-aux/deb/build.sh",
        "build-aux/rpm/build.sh",
        "build-aux/arch/build.sh",
    ):
        if files[script].count(OS_RELEASE) != 1:
            found.append(f"{script}: does not save the os-release of the container that ran the tests")
    if [
        line for line in code(files["build-aux/deb/Dockerfile"].splitlines()) if re.match(r"(FROM|ARG BASE)\b", line)
    ] != [
        "ARG BASE=ubuntu:26.04",
        "FROM ${BASE}",
    ]:
        found.append("build-aux/deb/Dockerfile: the build image is not the BASE build.sh names")

    # CI runs the package jobs whenever it runs Source, and gates on the proof.
    ci = jobs(files[".github/workflows/ci.yml"])
    for caller in ("source", "deb", "rpm-arch", "meson-tests"):
        # These produce the proofs the unconditional coverage gate requires.
        # No path output, detection result or upstream job may suppress them.
        block = ci[caller]
        if re.search(r"^    (?:if|needs):", block, re.M):
            found.append(f"ci.yml: {caller} depends on selection or another job")
    # The job's own lines: the comment that follows introduces the next job.
    if "\n" + ci.get("pytest-coverage", "").split("\n  #")[0].rstrip("\n") + "\n" != GATE:
        found.append("ci.yml: the Pytest coverage gate is missing or changed")
    always = json.loads(files["build-aux/ci/pr-paths.json"])["always"]
    if not {"source", "packages"} <= set(always):
        found.append("pr-paths.json: Source and the packages are not both always run")
    workflows = {
        "deb": jobs(files[".github/workflows/deb.yml"])["build"],
        "rpm": jobs(files[".github/workflows/packages.yml"])["rpm"],
        "arch": jobs(files[".github/workflows/packages.yml"])["arch"],
    }
    named = set()
    for image, job in skipped.items():
        if job == f"Debian package / Build .deb ({image})":
            ok = re.search(rf'matrix:\n        base: \[[^\]]*"{re.escape(image)}"', workflows["deb"])
            named.add("deb")
        elif job == f"RPM and Arch packages / Build .rpm ({image})":
            ok = f"          - base: {image}\n" in workflows["rpm"]
            named.add("rpm")
        elif job == "RPM and Arch packages / Build Arch package":
            ok = f"BASE=${{1:-{image}}}" in files["build-aux/arch/build.sh"]
            named.add("arch")
        else:
            ok = False
        if not ok:
            found.append(f"{image}: pytest-in {job!r} is not a package job built from that image")

    # The named jobs are required, build with checks on, and prove the run.
    for kind in sorted(named):
        block, (call, script, command) = workflows[kind], BUILDS[kind]
        if "continue-on-error" in block:
            found.append(f"{kind}: the job that runs Source's pytest has continue-on-error")
        if re.search(r"^    if:", block, re.M) or "exclude:" in block or (kind == "deb" and "include:" in block):
            found.append(f"{kind}: the job or its matrix can leave a build out")
        if not re.search(rf"\n      - run: {re.escape(call)}\n(?!        )", block):
            found.append(f"{kind}: the job does not run {call} unconditionally")
        if not re.search(re.escape(CHECK_STEP) + r"(?!        )", block):
            found.append(f"{kind}: no unconditional check_pytest_ran.py step and pytest-ran marker")
        if 'echo "CASSINI_TEST_LOGS=$RUNNER_TEMP/meson-logs" >> "$GITHUB_ENV"' not in block:
            found.append(f"{kind}: CASSINI_TEST_LOGS is not set")
        text = files[script]
        if code(text.splitlines()).count("  " + command) != 1:
            found.append(f"{kind}: {script} does not run `{command}` exactly once")
        if re.search(r"nocheck|DEB_BUILD_OPTIONS|DEB_BUILD_PROFILES|!check|PYTEST_ADDOPTS", text):
            found.append(f"{kind}: {script} can turn the check off or narrow it")
    for name in (".github/workflows/deb.yml", ".github/workflows/packages.yml"):
        if re.search(r"nocheck|DEB_BUILD_OPTIONS|DEB_BUILD_PROFILES|PYTEST_ADDOPTS", files[name]):
            found.append(f"{name} can turn the package check off or narrow it")
    if 'set --\n  [ ! -d /logs ] || set -- --define "cassini_test_logs /logs"\n' not in files["build-aux/rpm/build.sh"]:
        found.append("rpm: rpmbuild gets arguments other than the test-log define")

    # The recipes run the whole Meson test set, GTK required, and fail on failure.
    rules = files["debian/rules"]
    # The target up to the next blank line: exactly the four lines, nothing more.
    block = rules[rules.find("override_dh_auto_test:") :].split("\n\n")[0]
    if rules.count("dh_auto_test") != 1 or code(block.splitlines()) != RULES_CHECK:
        found.append("debian/rules: dh_auto_test is not just the full `meson test` with GTK required")
    if [line for line in code(rules.splitlines()) if re.match(r"-?include\b", line)] != [
        "include /usr/share/dpkg/architecture.mk"
    ]:
        found.append("debian/rules: includes a makefile this guard does not read")
    recipes = {
        "debian/rules": rules,
        "debian/clean": files["debian/clean"],
        "build-aux/rpm/cassini-talk.spec": files["build-aux/rpm/cassini-talk.spec"],
        "build-aux/arch/PKGBUILD": files["build-aux/arch/PKGBUILD"],
    }
    for name, text in recipes.items():
        if any(re.search(r"(^|[\s/'\"])tests\b", line) for line in code(text.splitlines())):
            found.append(f"{name}: touches tests/ outside the test command")
    spec = files["build-aux/rpm/cassini-talk.spec"]
    check = section(spec, r"^%check$", r"^%(files|changelog|install|build|prep|post|pre|clean)\b")
    if (
        len(re.findall(r"^%check$", spec, re.M)) != 1
        or check is None
        or code(check.splitlines())[1:3] != SPEC_CHECK
        or "||" in check
        or re.search(r"%(define|global)\s+(meson_test|__meson)|nocheck|without_check|bcond", spec)
    ):
        found.append("cassini-talk.spec: %check is not the full %meson_test with GTK required")
    pkgbuild = files["build-aux/arch/PKGBUILD"]
    check = section(pkgbuild, r"^check\(\) \{$", r"^\}$")
    if (
        check is None
        or [line.strip() for line in code(check.splitlines() + ["}"])] != ARCH_CHECK
        or re.search(r"BUILDENV|!check", pkgbuild)
    ):
        found.append("PKGBUILD: check() is not the full `meson test` with GTK required")

    # The check dependencies the GTK application tests need.
    control = deb_build_depends(files["debian/control"])
    missing = [d for d in DEB_DEPS if control.get(d) not in ("", "<!nocheck>")]
    if missing:
        found.append(f"debian/control: missing check dependencies {missing}")
    requires = spec_requires(spec)
    for branch, wanted in (("common", RPM_DEPS), ("openSUSE", SUSE_DEPS), ("Fedora", FEDORA_DEPS)):
        if missing := sorted(set(wanted) - requires[branch]):
            found.append(f"cassini-talk.spec ({branch}): missing BuildRequires {missing}")
    arch = arch_array(pkgbuild, "depends") | arch_array(pkgbuild, "makedepends") | arch_array(pkgbuild, "checkdepends")
    if missing := sorted(set(ARCH_DEPS) - arch):
        found.append(f"PKGBUILD: missing dependencies {missing}")

    # pytest's own configuration selects nothing narrower than tests/.
    if tomllib.loads(files["pyproject.toml"])["tool"]["pytest"]["ini_options"] != PYTEST_OPTIONS:
        found.append("pyproject.toml: the pytest options changed (addopts, testpaths, python_files ...)")
    if "collect_ignore" in files["tests/conftest.py"]:
        found.append("tests/conftest.py: collect_ignore leaves test files out")
    for name in ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "conftest.py"):
        if files[name]:
            found.append(f"{name}: pytest reads it before pyproject.toml")
    if "[pytest]" in files["tox.ini"] or "[tool:pytest]" in files["setup.cfg"]:
        found.append("tox.ini or setup.cfg configures pytest")

    # No mechanism that edits the sources (tests included) inside a build.
    if code(files["debian/patches/series"].splitlines()):
        found.append("debian/patches: the Debian build patches the sources")
    if re.search(r"^Patch\d*:", files["build-aux/rpm/cassini-talk.spec"], re.M):
        found.append("cassini-talk.spec: the RPM build patches the sources")
    if any(re.search(r"(^|\s)patch\s", line) for line in code(files["build-aux/arch/PKGBUILD"].splitlines())):
        found.append("PKGBUILD: the Arch build patches the sources")
    return found


def test_source_skips_pytest_only_where_a_required_package_job_runs_all_of_it():
    assert problems(read()) == []


def test_the_runtime_check_knows_the_registered_shard_command():
    # check_pytest_ran.py compares each shard's command with src/meson.build's.
    spec = importlib.util.spec_from_file_location("check", ROOT / "build-aux/ci/check_pytest_ran.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    meson = (ROOT / "src/meson.build").read_text()
    registered = re.search(r"args: \[('-m', 'pytest'.*?)'--junitxml=", meson, re.S)[1]
    assert re.findall(r"'([^']+)'", registered) == check.ARGUMENTS
    assert "meson.project_source_root() / 'tests'],\n" in meson
    assert check.shard_count() == int(re.search(r"^pytest_shards = (\d+)$", meson, re.M)[1])


MUTATIONS = {
    "source matrix excluded": (
        "source.yml",
        "      matrix:\n",
        "      matrix:\n        exclude:\n          - image: fedora:latest\n",
    ),
    "source matrix job filtered": (
        "source.yml",
        "  distros:\n",
        "  distros:\n    if: false\n",
    ),
    "source gated by false or missing changes output": (
        "ci.yml",
        "    uses: ./.github/workflows/source.yml",
        "    if: needs.changes.outputs.source != 'false'\n    uses: ./.github/workflows/source.yml",
    ),
    "meson gated by changes": (
        "ci.yml",
        "    uses: ./.github/workflows/meson-tests.yml",
        "    needs: changes\n    if: needs.changes.outputs.packages == 'true'\n    uses: ./.github/workflows/meson-tests.yml",
    ),
    # The review's case, and the other ways to narrow or drop the Debian run.
    "deb --no-suite pytest": ("debian/rules", "--print-errorlogs", "--print-errorlogs --no-suite pytest"),
    "deb --suite": ("debian/rules", "--print-errorlogs", "--print-errorlogs --suite conformance"),
    "deb one test": ("debian/rules", "--print-errorlogs", "--print-errorlogs pytest-1"),
    "deb no GTK guard": ("debian/rules", "\tCASSINI_TEST_REQUIRE_GTK=1 meson", "\tmeson"),
    "deb ignore errors": ("debian/rules", "\tCASSINI_TEST_REQUIRE_GTK=1 meson", "\t-CASSINI_TEST_REQUIRE_GTK=1 meson"),
    "deb || true": ("debian/rules", "--print-errorlogs\nendif", "--print-errorlogs || true\nendif"),
    "deb test emptied": (
        "debian/rules",
        "\tCASSINI_TEST_REQUIRE_GTK=1 meson test",
        "\t: CASSINI_TEST_REQUIRE_GTK=1 meson test",
    ),
    "deb second override": (
        "debian/rules",
        "override_dh_auto_test:\n",
        "override_dh_auto_test:\n\ttrue\n\noverride_dh_auto_test:\n",
    ),
    "deb nocheck": ("build-aux/deb/build.sh", "  dpkg-buildpackage", "  DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage"),
    "deb nocheck profile": ("build-aux/deb/build.sh", "-us -uc -b", "-us -uc -b -Pnocheck"),
    "deb continue-on-error": (
        "deb.yml",
        "    timeout-minutes: 30\n",
        "    timeout-minutes: 30\n    continue-on-error: true\n",
    ),
    "deb check step dropped": ("deb.yml", CHECK_STEP, "\n"),
    "deb check step conditional": ("deb.yml", CHECK_STEP, CHECK_STEP + "        if: false\n"),
    "deb PYTEST_ADDOPTS": (
        "deb.yml",
        "env:\n  CONTAINER_ENGINE: docker",
        "env:\n  PYTEST_ADDOPTS: -k nothing\n  CONTAINER_ENGINE: docker",
    ),
    "deb without adw": ("debian/control", "               gir1.2-adw-1 <!nocheck>,\n", ""),
    "deb without gtk": ("debian/control", "               gir1.2-gtk-4.0 <!nocheck>,\n", ""),
    "deb gtk only in another profile": (
        "debian/control",
        "gir1.2-gtk-4.0 <!nocheck>",
        "gir1.2-gtk-4.0 <pkg.cassini.never>",
    ),
    "deb image gone": ("deb.yml", '["ubuntu:26.04", "debian:testing"]', '["ubuntu:26.04"]'),
    "rpm --no-suite": (
        "build-aux/rpm/cassini-talk.spec",
        "%meson_test --num-processes=2\n",
        "%meson_test --num-processes=2 --no-suite pytest\n",
    ),
    "rpm || :": (
        "build-aux/rpm/cassini-talk.spec",
        "%meson_test --num-processes=2\n",
        "%meson_test --num-processes=2 || :\n",
    ),
    "rpm unbounded workers": (
        "build-aux/rpm/cassini-talk.spec",
        "%meson_test --num-processes=2\n",
        "%meson_test\n",
    ),
    "rpm no GTK guard": ("build-aux/rpm/cassini-talk.spec", "export CASSINI_TEST_REQUIRE_GTK=1\n", ""),
    "rpm meson_test redefined": ("build-aux/rpm/cassini-talk.spec", "%check\n", "%define meson_test true\n%check\n"),
    "rpm --nocheck": ("build-aux/rpm/build.sh", '"$@" "$spec"', '"$@" --nocheck "$spec"'),
    "rpm extra args": ("build-aux/rpm/build.sh", "  set --\n", "  set -- --without check\n"),
    "rpm suse without gtk": ("build-aux/rpm/cassini-talk.spec", "BuildRequires:  typelib-1_0-Gtk-4_0\n", ""),
    "rpm fedora without adw": ("build-aux/rpm/cassini-talk.spec", "BuildRequires:  libadwaita\n", ""),
    "rpm continue-on-error": ("packages.yml", "  rpm:\n    name:", "  rpm:\n    continue-on-error: true\n    name:"),
    "rpm check step dropped": ("packages.yml", CHECK_STEP, "\n", 0),
    "tumbleweed image gone": (
        "packages.yml",
        "          - base: registry.opensuse.org/opensuse/tumbleweed:latest\n",
        "          - base: opensuse/leap:16.0\n",
    ),
    "arch --no-suite": (
        "build-aux/arch/PKGBUILD",
        "-C build --num-processes=2 --print-errorlogs",
        "-C build --num-processes=2 --print-errorlogs --no-suite pytest",
    ),
    "arch no GTK guard": ("build-aux/arch/PKGBUILD", "  CASSINI_TEST_REQUIRE_GTK=1 meson", "  meson"),
    "arch || true": (
        "build-aux/arch/PKGBUILD",
        "-C build --num-processes=2 --print-errorlogs\n}",
        "-C build --num-processes=2 --print-errorlogs || true\n}",
    ),
    "arch --nocheck": ("build-aux/arch/build.sh", '"makepkg -s --noconfirm"', '"makepkg -s --noconfirm --nocheck"'),
    "arch BUILDENV": ("build-aux/arch/PKGBUILD", "options=(", "BUILDENV=(!check)\noptions=("),
    "arch without libadwaita": ("build-aux/arch/PKGBUILD", "  'libadwaita>=1.8'\n", ""),
    "arch check step dropped": ("packages.yml", CHECK_STEP, "\n", 1),
    "arch image moved": ("build-aux/arch/build.sh", "BASE=${1:-archlinux:latest}", "BASE=${1:-archlinux:base}"),
    "source keeps fedora skip": (
        "source.yml",
        "          - image: fedora:latest\n",
        '          - image: fedora:latest\n            pytest-in: "RPM and Arch packages / Build .rpm (fedora:latest)"\n',
    ),
    "source names a wrong job": (
        "source.yml",
        '"Debian package / Build .deb (debian:testing)"',
        '"Debian package / Build .deb (ubuntu:26.04)"',
    ),
    "source skips unconditionally": ("source.yml", "${{ matrix.pytest-in && '1' || '' }}", "'1'"),
    # The red-team bypasses of 9a8b13e, each confirmed by an independent run.
    "deb hook deletes tests": (
        "debian/rules",
        "override_dh_gencontrol:",
        "execute_before_dh_auto_test:\n\trm -f tests/test_record_toggle.py\n\noverride_dh_gencontrol:",
    ),
    "deb hook after the test": (
        "debian/rules",
        "override_dh_gencontrol:",
        "execute_after_dh_auto_test:\n\ttrue\n\noverride_dh_gencontrol:",
    ),
    "deb includes a makefile": (
        "debian/rules",
        "include /usr/share/dpkg/architecture.mk\n",
        "include /usr/share/dpkg/architecture.mk\n-include debian/local.mk\n",
    ),
    "deb clean list": (
        "debian/control",
        "Build-Depends:",
        "Build-Depends:",
        0,
        ("debian/clean", "tests/test_record_toggle.py\n"),
    ),
    "rpm %prep deletes tests": (
        "build-aux/rpm/cassini-talk.spec",
        "%autosetup -p1\n",
        "%autosetup -p1\nrm -f tests/test_record_toggle.py\n",
    ),
    "rpm numpy behind %if": (
        "build-aux/rpm/cassini-talk.spec",
        "BuildRequires:  python3dist(numpy)\n",
        "%if 0%{?fedora}\nBuildRequires:  python3dist(numpy)\n%endif\n",
    ),
    "arch prepare deletes tests": (
        "build-aux/arch/PKGBUILD",
        "build() {\n",
        'prepare() {\n  rm -f "$_srcdir"/tests/test_record_toggle.py\n}\n\nbuild() {\n',
    ),
    "arch options !check": ("build-aux/arch/PKGBUILD", "options=(!strip !debug)", "options=(!strip !debug !check)"),
    "deb job if": (
        "deb.yml",
        "    timeout-minutes: 30\n",
        "    timeout-minutes: 30\n    if: github.event.pull_request.draft == false\n",
    ),
    "rpm job if": (
        "packages.yml",
        "  rpm:\n    name:",
        "  rpm:\n    if: github.event_name != 'pull_request'\n    name:",
    ),
    "arch job if": ("packages.yml", "  arch:\n    name:", "  arch:\n    if: github.event_name == 'push'\n    name:"),
    "deb matrix exclude": (
        "deb.yml",
        '["ubuntu:26.04", "debian:testing"]\n',
        '["ubuntu:26.04", "debian:testing"]\n        exclude:\n          - base: debian:testing\n',
    ),
    "deb build step conditional": (
        "deb.yml",
        '      - run: build-aux/deb/build.sh "${{ matrix.base }}"\n',
        '      - run: build-aux/deb/build.sh "${{ matrix.base }}"\n        if: false\n',
    ),
    "ran marker conditional": (
        "packages.yml",
        "          if-no-files-found: error\n",
        "          if-no-files-found: error\n        if: false\n",
        1,
    ),
    "ci deb caller only on push": (
        "ci.yml",
        "    uses: ./.github/workflows/deb.yml",
        "    if: github.event_name == 'push'\n    uses: ./.github/workflows/deb.yml",
    ),
    "ci rpm-arch caller waits for build": (
        "ci.yml",
        "  rpm-arch:\n    name: RPM and Arch packages\n",
        "  rpm-arch:\n    name: RPM and Arch packages\n    needs: [changes, build]\n",
    ),
    "ci gate dropped": ("ci.yml", "  pytest-coverage:\n", "  pytest-coverage-disabled:\n"),
    "ci gate does not wait for deb": (
        "ci.yml",
        "    needs: [source, deb, rpm-arch, meson-tests]\n",
        "    needs: [source, rpm-arch, meson-tests]\n",
    ),
    "ci gate does not wait for the Meson tests": (
        "ci.yml",
        "    needs: [source, deb, rpm-arch, meson-tests]\n",
        "    needs: [source, deb, rpm-arch]\n",
    ),
    "ci gate accepts skipped Meson tests": (
        "ci.yml",
        'test "$MESON_TESTS" = success ||',
        'test "$MESON_TESTS" != failure ||',
    ),
    "pr-paths packages not always": ("build-aux/ci/pr-paths.json", '    "packages",\n', ""),
    "source fedora single-quoted pytest-in": (
        "source.yml",
        "          - image: fedora:latest\n",
        "          - image: fedora:latest\n            pytest-in: 'RPM and Arch packages / Build .rpm (fedora:44)'\n",
    ),
    "source pytest record dropped": (
        "source.yml",
        "          name: pytest-${{ steps.pytest.outputs.kind }}-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}\n",
        "          name: pytest-nodes-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}\n",
    ),
    "source uploads marker after failed build": (
        "source.yml",
        "        id: pytest\n        run: |\n          . build",
        "        id: pytest\n        if: ${{ !cancelled() }}\n        run: |\n          . build",
    ),
    "source drops run_attempt suffix": (
        "source.yml",
        "          name: pytest-${{ steps.pytest.outputs.kind }}-${{ steps.pytest.outputs.slug }}-attempt-${{ github.run_attempt }}\n",
        "          name: pytest-${{ steps.pytest.outputs.kind }}-${{ steps.pytest.outputs.slug }}\n",
    ),
    "source omits --install-outcome": (
        "source.yml",
        '            --install-outcome "$CASSINI_INSTALL_OUTCOME" --slug',
        "            --slug",
    ),
    "source shares meson-logs with install outcome": (
        "source.yml",
        'echo "CASSINI_INSTALL_OUTCOME=$RUNNER_TEMP/install-outcome" >> "$GITHUB_ENV"',
        'echo "CASSINI_INSTALL_OUTCOME=$RUNNER_TEMP/meson-logs" >> "$GITHUB_ENV"',
    ),
    "source continue-on-error": (
        "source.yml",
        "    timeout-minutes: 30\n",
        "    timeout-minutes: 30\n    continue-on-error: true\n",
    ),
    "check-distros skips fedora on its own": (
        "build-aux/check-distros.sh",
        ". build-aux/container.sh\n",
        '. build-aux/container.sh\ncase "$*" in *fedora*) CASSINI_SKIP_PYTEST=1 ;; esac\n',
    ),
    "deb Dockerfile ignores BASE": (
        "build-aux/deb/Dockerfile",
        "ARG BASE=ubuntu:26.04\nFROM ${BASE}",
        "ARG BASE_IMAGE=ubuntu:26.04\nFROM ${BASE_IMAGE}",
    ),
    "deb os-release not saved": ("build-aux/deb/build.sh", "    " + OS_RELEASE + "\n", ""),
    "source os-release not saved": ("build-aux/check-distros.sh", "  " + OS_RELEASE + "\n", ""),
    "root pytest.ini": (
        "pyproject.toml",
        "[tool.pytest.ini_options]",
        "[tool.pytest.ini_options]",
        0,
        ("pytest.ini", "[pytest]\naddopts = -k 'not record'\n"),
    ),
    "root conftest": (
        "pyproject.toml",
        "[tool.pytest.ini_options]",
        "[tool.pytest.ini_options]",
        0,
        ("conftest.py", "collect_ignore_glob = ['tests/test_record*']\n"),
    ),
    "setup.cfg pytest section": (
        "pyproject.toml",
        "[tool.pytest.ini_options]",
        "[tool.pytest.ini_options]",
        0,
        ("setup.cfg", "[tool:pytest]\naddopts = -k x\n"),
    ),
    "debian patch series": (
        "debian/control",
        "Build-Depends:",
        "Build-Depends:",
        0,
        ("debian/patches/series", "drop-record-tests.patch\n"),
    ),
    "rpm Patch": (
        "build-aux/rpm/cassini-talk.spec",
        "BuildRequires:  meson >= 1.3\n",
        "Patch0:         drop-record-tests.patch\nBuildRequires:  meson >= 1.3\n",
    ),
    "arch patch": (
        "build-aux/arch/PKGBUILD",
        "build() {\n",
        "prepare() {\n  patch -p1 -i ../drop.patch\n}\n\nbuild() {\n",
    ),
    "source skips on its own": (
        "source.yml",
        '      - run: build-aux/check-distros.sh "${{ matrix.image }}"\n',
        '      - run: build-aux/check-distros.sh "${{ matrix.image }}"\n        env:\n          CASSINI_SKIP_PYTEST: "1"\n',
    ),
    "pyproject addopts": (
        "pyproject.toml",
        'testpaths = ["tests"]\n',
        'testpaths = ["tests"]\naddopts = "--ignore=tests/test_record_toggle.py"\n',
    ),
    "pyproject testpaths": ("pyproject.toml", 'testpaths = ["tests"]\n', 'testpaths = ["tests/ux"]\n'),
    "conftest collect_ignore": (
        "tests/conftest.py",
        "TESTS = Path(",
        'collect_ignore = ["test_record_toggle.py"]\nTESTS = Path(',
    ),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_every_way_to_narrow_or_drop_the_replacement_run_fails_the_guard(name):
    path, old, new, *rest = MUTATIONS[name]
    occurrence, extra = (rest + [None, None])[:2]
    occurrence = [] if occurrence is None else [occurrence]
    path = next(f for f in FILES if f == path or f.endswith("/" + path))
    files = read()
    if extra:  # A file this guard reads when present.
        files[extra[0]] = extra[1]
    text = files[path]
    starts = [m.start() for m in re.finditer(re.escape(old), text)]
    assert starts and (occurrence or len(starts) == 1), (name, len(starts))  # The mutation still applies.
    at = starts[occurrence[0]] if occurrence else starts[0]
    files[path] = text[:at] + new + text[at + len(old) :]
    assert set(problems(files)) - set(problems(read())), name  # A new problem, not an old one.


D82_FILES = (
    "meson_options.txt",
    "src/meson.build",
    "debian/rules",
    "build-aux/rpm/cassini-talk.spec",
    "build-aux/arch/PKGBUILD",
    "build-aux/check-distros.sh",
    ".github/workflows/meson-tests.yml",
)


def d82_parallel_problems(texts):
    issues = []
    if "option('pytest_parallel', type: 'boolean', value: false," not in texts["meson_options.txt"]:
        issues.append("meson_options.txt must keep pytest_parallel defaulting to false")
    if "is_parallel: get_option('pytest_parallel')," not in texts["src/meson.build"]:
        issues.append("src/meson.build must wire pytest shards to get_option('pytest_parallel')")
    for rel in D82_FILES[2:]:
        content = texts[rel]
        if "-Dpytest_parallel=true" not in content:
            issues.append(f"{rel}: missing -Dpytest_parallel=true")
        if "--num-processes=2" not in content:
            issues.append(f"{rel}: missing bounded --num-processes=2")
    return issues


def test_d82_bounded_two_worker_pytest_and_fedora_required_deps():
    import subprocess

    assert not (ROOT / ".github/workflows/pytest-ab.yml").exists()
    texts = {rel: (ROOT / rel).read_text() for rel in D82_FILES}
    assert d82_parallel_problems(texts) == []

    d82_mutations = [
        (
            "meson_options.txt",
            "option('pytest_parallel', type: 'boolean', value: false,",
            "option('pytest_parallel', type: 'boolean', value: true,",
        ),
        ("src/meson.build", "get_option('pytest_parallel')", "false"),
        *((rel, token, "") for rel in D82_FILES[2:] for token in ("-Dpytest_parallel=true", "--num-processes=2")),
    ]
    for rel, old, new in d82_mutations:
        mutated = dict(texts)
        assert mutated[rel].count(old) == 1, (rel, old)
        mutated[rel] = mutated[rel].replace(old, new, 1)
        assert d82_parallel_problems(mutated), (rel, old)

    help_out = subprocess.check_output(["sh", str(ROOT / "build-aux/install-deps.sh"), "--help"], text=True)
    assert "--required-only" in help_out and "shellcheck" not in help_out
    distros = (ROOT / "build-aux/check-distros.sh").read_text()
    assert "build-aux/install-deps.sh --all --yes --required-only" in distros
    assert "build-aux/install-deps.sh --all --yes\n" in distros
