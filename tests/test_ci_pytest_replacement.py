# SPDX-License-Identifier: GPL-3.0-or-later
"""G11 (D20): a Source distro job skips pytest only where a required package
job runs the complete suite in the same image.

`problems` reads the files that decide it; every mutation below must make it
fail. At run time build-aux/ci/check_pytest_ran.py checks the package job's
Meson logs for every shard (tests/test_check_pytest_ran.py).
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FILES = (
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
)
# The Source images that keep their pytest suite: no package job uses them.
KEEPS_PYTEST = {"fedora:latest", "alpine:edge"}

RULES_CHECK = [
    "override_dh_auto_test:",
    "ifeq (,$(filter nocheck,$(DEB_BUILD_OPTIONS)))",
    "\tCASSINI_TEST_REQUIRE_GTK=1 meson test -C obj-$(DEB_HOST_GNU_TYPE) --print-errorlogs",
    "endif",
]
SPEC_CHECK = ["export CASSINI_TEST_REQUIRE_GTK=1", "%meson_test"]
ARCH_CHECK = ["check() {", "CASSINI_TEST_REQUIRE_GTK=1 meson test -C build --print-errorlogs", "}"]
CHECK_STEP = (
    "\n      - name: Require the whole pytest suite to have run and passed\n"
    '        run: python3 build-aux/ci/check_pytest_ran.py "$CASSINI_TEST_LOGS"\n'
)
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
    return {name: (ROOT / name).read_text() for name in FILES}


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


def spec_requires(text):
    return {m[1] for m in re.finditer(r"^BuildRequires:\s+(\S+)", text, re.M)}


def arch_array(pkgbuild, name):
    body = re.search(rf"^{name}=\((.*?)\)", re.sub(r"#[^\n]*", "", pkgbuild), re.M | re.S)[1]
    words = body.split()
    return {re.split(r"[<>=]", word.strip("'\""))[0] for word in words}


def problems(files):
    found = []
    source = jobs(files[".github/workflows/source.yml"])["distros"]
    entries = re.findall(r'^          - image: (\S+)\n(?:            pytest-in: "([^"]+)"\n)?', source, re.M)
    skipped = {image: job for image, job in entries if job}
    if {image for image, job in entries if not job} != KEEPS_PYTEST:
        found.append(f"Source keeps pytest on {sorted(i for i, j in entries if not j)}, not {sorted(KEEPS_PYTEST)}")
    if "CASSINI_SKIP_PYTEST: ${{ matrix.pytest-in && '1' || '' }}" not in source:
        found.append("Source skips pytest without a pytest-in job")
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
        if not re.search(rf"\n      - run: {re.escape(call)}\n", block):
            found.append(f"{kind}: the job does not run {call}")
        if not re.search(re.escape(CHECK_STEP) + r"(?!        )", block):
            found.append(f"{kind}: no unconditional check_pytest_ran.py step")
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
    if rules.count("override_dh_auto_test") != 1 or code(block.splitlines()) != RULES_CHECK:
        found.append("debian/rules: override_dh_auto_test is not the full `meson test` with GTK required")
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
        or "BUILDENV" in pkgbuild
    ):
        found.append("PKGBUILD: check() is not the full `meson test` with GTK required")

    # The check dependencies the GTK application tests need.
    control = deb_build_depends(files["debian/control"])
    missing = [d for d in DEB_DEPS if control.get(d) not in ("", "<!nocheck>")]
    if missing:
        found.append(f"debian/control: missing check dependencies {missing}")
    head, _, rest = spec.partition("%if 0%{?suse_version}\n")
    suse, _, fedora = rest.partition("%else\n")
    fedora = fedora.partition("%endif\n")[0]
    for branch, text, wanted in (
        ("common", head, RPM_DEPS),
        ("openSUSE", suse, SUSE_DEPS),
        ("Fedora", fedora, FEDORA_DEPS),
    ):
        if missing := sorted(set(wanted) - spec_requires(text)):
            found.append(f"cassini-talk.spec ({branch}): missing BuildRequires {missing}")
    arch = arch_array(pkgbuild, "depends") | arch_array(pkgbuild, "makedepends") | arch_array(pkgbuild, "checkdepends")
    if missing := sorted(set(ARCH_DEPS) - arch):
        found.append(f"PKGBUILD: missing dependencies {missing}")
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
    "rpm --no-suite": ("build-aux/rpm/cassini-talk.spec", "%meson_test\n", "%meson_test --no-suite pytest\n"),
    "rpm || :": ("build-aux/rpm/cassini-talk.spec", "%meson_test\n", "%meson_test || :\n"),
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
        "-C build --print-errorlogs",
        "-C build --print-errorlogs --no-suite pytest",
    ),
    "arch no GTK guard": ("build-aux/arch/PKGBUILD", "  CASSINI_TEST_REQUIRE_GTK=1 meson", "  meson"),
    "arch || true": (
        "build-aux/arch/PKGBUILD",
        "-C build --print-errorlogs\n}",
        "-C build --print-errorlogs || true\n}",
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
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_every_way_to_narrow_or_drop_the_replacement_run_fails_the_guard(name):
    path, old, new, *occurrence = MUTATIONS[name]
    path = next(f for f in FILES if f == path or f.endswith("/" + path))
    files = read()
    text = files[path]
    starts = [m.start() for m in re.finditer(re.escape(old), text)]
    assert starts and (occurrence or len(starts) == 1), (name, len(starts))  # The mutation still applies.
    at = starts[occurrence[0]] if occurrence else starts[0]
    files[path] = text[:at] + new + text[at + len(old) :]
    assert set(problems(files)) - set(problems(read())), name  # A new problem, not an old one.
