# SPDX-License-Identifier: GPL-3.0-or-later
"""F: the Flatpak job builds the host Trash portal once per input (a main-saved
cache), and decodes the engine goldens on the stable bundle only (D20)."""

import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FLATPAK = ROOT / ".github/workflows/flatpak.yml"
SCRIPT = ROOT / "tests/flatpak/prepare_trash_portal.py"
MAIN_ONLY = "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"


def steps():
    return re.split(r"\n(?=      - )", FLATPAK.read_text())[1:]


def field(step, name):
    match = re.search(rf"^\s+{name}: (.+)$", step, re.M)
    return match and match[1].strip()


def index(predicate):
    (found,) = [i for i, step in enumerate(steps()) if predicate(step)]
    return found


def test_the_trash_portal_is_restored_built_and_saved_from_main_only_at_one_path():
    all_steps = steps()
    restore = index(lambda s: "uses: actions/cache/restore@" in s and "_ci-trash-portal" in s)
    prepare = index(lambda s: "prepare_trash_portal.py --output" in s)
    save = index(lambda s: "uses: actions/cache/save@" in s and "_ci-trash-portal" in s)
    devel = index(lambda s: "smoke.sh cassini-talk-devel.flatpak" in s)
    assert restore + 1 == prepare and prepare + 1 == save and save < devel
    assert field(all_steps[restore], "id") == "trash-portal"
    # The same tree everywhere: restored, built (or kept) and saved at the path
    # prepare_trash_portal.py writes into its marker and rpath, then used by smoke.sh.
    assert field(all_steps[restore], "path") == field(all_steps[save], "path") == "_ci-trash-portal"
    assert 'prepare_trash_portal.py --output "$PWD/_ci-trash-portal"' in all_steps[prepare]
    assert 'portal_prefix="$PWD/_ci-trash-portal"' in (ROOT / "build-aux/flatpak/smoke.sh").read_text()
    # The prepare step always runs: a hit is used only if its marker matches.
    assert field(all_steps[prepare], "if") is None
    assert field(all_steps[save], "if") == MAIN_ONLY + " && steps.trash-portal.outputs.cache-hit != 'true'"
    assert field(all_steps[save], "key") == "${{ steps.trash-portal.outputs.cache-primary-key }}"
    assert not any("uses: actions/cache@" in step and "_ci-trash-portal" in step for step in all_steps)


def test_the_trash_portal_key_names_every_input_of_its_build():
    key = field(steps()[index(lambda s: "id: trash-portal" in s)], "key")
    digest = re.search(r"^      image: \S+@sha256:([0-9a-f]{64})$", FLATPAK.read_text(), re.M)[1]
    # The script holds the pins and build flags; the container image is the SDK
    # and toolchain it compiles against; the runner's arch and kind come last.
    assert key == (
        "trash-portal-${{ runner.arch }}-${{ runner.environment }}-"
        + digest[:16]
        + "-${{ hashFiles('tests/flatpak/prepare_trash_portal.py') }}"
    )
    # Hashing the script alone covers its inputs: it reads no other file of the
    # repository, and every download is pinned by version and SHA-256 inside it.
    text = SCRIPT.read_text()
    assert not re.search(r"build-aux|meson\.build|tests/|src/", text)
    for pin in ("VERSION", "SHA256", "FUSE_VERSION", "FUSE_SHA256"):
        assert re.search(rf"^{pin} = \"[0-9a-f.]+\"$", text, re.M), pin


@pytest.fixture
def portal(monkeypatch, tmp_path):
    """prepare_trash_portal with the downloads and meson replaced: each meson
    install writes what the real one installs that the smoke uses."""
    spec = importlib.util.spec_from_file_location("prepare_trash_portal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archives = {}
    for name, top in (("fuse", f"fuse-{module.FUSE_VERSION}"), ("portal", f"xdg-desktop-portal-{module.VERSION}")):
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(f"{top}/meson.build")
            data = b"project('x')\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        archives[name] = archive
    monkeypatch.setattr(
        module, "fetch", lambda cache, name, url, digest: archives["fuse" if "fuse" in name else "portal"]
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command[:2])
        if command[:2] == ["meson", "install"]:
            prefix = calls_prefix[0]
            if Path(command[3]).name == "fuse-build":
                (prefix / "lib").mkdir(parents=True, exist_ok=True)
                (prefix / "lib/libfuse3.so.3.18.3").write_bytes(b"\x7fELF fuse")
                (prefix / "lib/libfuse3.so.4").unlink(missing_ok=True)
                (prefix / "lib/libfuse3.so.4").symlink_to("libfuse3.so.3.18.3")
            else:
                (prefix / "libexec").mkdir(parents=True, exist_ok=True)
                (prefix / "libexec/xdg-desktop-portal").write_bytes(b"\x7fELF portal")
                (prefix / "libexec/xdg-desktop-portal").chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    calls_prefix = []
    monkeypatch.setattr(module.subprocess, "run", run)

    def prepare(output):
        calls_prefix[:] = [output.resolve()]
        calls.clear()
        module.prepare(output, tmp_path / "downloads")
        return list(calls)

    return module, prepare


def snapshot(tree):
    """Every entry under tree: its type, mode, bytes or link target."""
    out = {}
    for path in sorted(tree.rglob("*")):
        relative = str(path.relative_to(tree))
        if path.is_symlink():
            out[relative] = ("link", os.readlink(path))
        elif path.is_file():
            out[relative] = ("file", path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            out[relative] = ("dir", path.stat().st_mode)
    return out


def cache_round_trip(tree):
    """What actions/cache does between main's save and a later restore: tar the
    path, drop the workspace, untar it at the same place."""
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as tar:
        tar.add(tree, arcname=tree.name)
    for path in sorted(tree.rglob("*"), reverse=True):
        path.unlink() if path.is_symlink() or path.is_file() else path.rmdir()
    tree.rmdir()
    packed.seek(0)
    with tarfile.open(fileobj=packed) as tar:
        # Our own fixture archive: keep its modes exactly (the "tar" filter drops
        # group-write, which a umask of 0002 sets, and the snapshot would differ).
        tar.extractall(tree.parent, filter="fully_trusted")


def test_a_restored_portal_is_used_as_built_and_anything_else_is_rebuilt(portal, tmp_path):
    module, prepare = portal
    workspace = tmp_path / "workspace"
    output = workspace / "_ci-trash-portal"
    builds = [["meson", "setup"], ["meson", "compile"], ["meson", "install"]] * 2
    assert prepare(output) == builds  # a cache miss: nothing there, so it builds
    marker = json.loads((output / "cassini-test-tool.json").read_text())
    assert marker["prefix"] == str(output.resolve())
    built = snapshot(output)
    assert {"libexec/xdg-desktop-portal", "lib/libfuse3.so.4", "cassini-test-tool.json"} <= set(built)
    # A hit: the same tree at the same path, and prepare runs no build at all.
    cache_round_trip(output)
    assert snapshot(output) == built
    assert prepare(output) == []
    assert snapshot(output) == built
    # The same tree at another prefix (its rpath would point elsewhere) is rebuilt.
    elsewhere = tmp_path / "other-workspace/_ci-trash-portal"
    elsewhere.parent.mkdir()
    output.rename(elsewhere)
    assert prepare(elsewhere) == builds
    # So is one whose marker names other pins, or that lost the portal.
    for change in ("pins", "binary"):
        cache_round_trip(elsewhere)
        if change == "pins":
            stale = dict(json.loads((elsewhere / "cassini-test-tool.json").read_text()), version="0.0.0")
            (elsewhere / "cassini-test-tool.json").write_text(json.dumps(stale))
        else:
            (elsewhere / "libexec/xdg-desktop-portal").unlink()
        assert prepare(elsewhere) == builds, change


# D20: the devel bundle skips the engines' golden decodes only while they are
# the stable bundle's: the same pinned sources, and code that never asks which
# profile or application it runs as.


def test_only_the_devel_smoke_skips_the_goldens_and_the_stable_one_runs_them_on_the_cached_model():
    all_steps = steps()
    devel = all_steps[index(lambda s: "smoke.sh cassini-talk-devel.flatpak" in s)]
    stable = all_steps[index(lambda s: "smoke.sh cassini-talk.flatpak" in s)]
    assert field(devel, "run") == (
        "build-aux/flatpak/smoke.sh cassini-talk-devel.flatpak io.codemyriad.CassiniTalk.Devel --without-goldens"
    )
    assert "build-aux/flatpak/smoke.sh cassini-talk.flatpak io.codemyriad.CassiniTalk\n" in stable + "\n"
    model = field(all_steps[index(lambda s: "key: parakeet-ultra-" in s)], "path")
    assert field(stable, "CASSINI_PARAKEET_TEST_CACHE") == model
    assert "CASSINI_PARAKEET_TEST_CACHE" not in devel
    # Nothing else drops the goldens: not local builds, not another workflow.
    users = [
        str(path.relative_to(ROOT))
        for path in [*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob("build-aux/**/*.sh")]
        if "--without-goldens" in path.read_text()
    ]
    assert sorted(users) == [".github/workflows/flatpak.yml", "build-aux/flatpak/smoke.sh"]


def test_the_devel_and_stable_manifests_differ_only_in_the_id_and_the_profile():
    devel, stable = (
        json.loads((ROOT / f"build-aux/flatpak/{name}.json").read_text())
        for name in ("io.codemyriad.CassiniTalk.Devel", "io.codemyriad.CassiniTalk")
    )
    assert (devel.pop("id"), stable.pop("id")) == ("io.codemyriad.CassiniTalk.Devel", "io.codemyriad.CassiniTalk")
    (app,) = [module for module in devel["modules"] if module["name"] == "cassini-talk"]
    options = app["config-opts"]
    options[options.index("-Dprofile=development")] = "-Dprofile=default"
    assert devel == stable
    # Every engine source is a pinned download, so the same manifest means the same bytes.
    files = [source for module in stable["modules"] for source in module["sources"] if source["type"] == "file"]
    assert files and all(re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) for source in files)


PROFILE_BRANCH = re.compile(r"""profile["')]*\s*[!=]=\s*["'](?:development|default)["']|FLATPAK_ID["')]*\s*[!=]=""")


def test_the_only_code_that_branches_on_the_profile_is_checked_on_both_bundles():
    # The devel window's style (Desktop launches the devel bundle) and browser
    # registration's protected build (companion_bridge.py runs on both bundles).
    # A new branch on the profile or the Flatpak ID fails here: decide whether the
    # devel bundle's goldens must come back before allowing it.
    found = sorted(
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path in (ROOT / "src").rglob("*.py")
        for line in path.read_text().splitlines()
        if PROFILE_BRANCH.search(line)
    )
    assert found == [
        'src/cassini_talk/main.py: if self.profile == "development":',
        'src/cassini_talk/registration.py: return os.environ.get("FLATPAK_ID") != "io.codemyriad.CassiniTalk"',
        'src/cassini_talk/registration.py: return value.get("profile") != "default" or value.get("bindir") '
        'not in ("/usr/bin", "/usr/local/bin")',
    ]
    assert "protected_build" not in "".join(
        (ROOT / f"src/cassini_talk/{name}.py").read_text()
        for name in ("whistle_worker", "parakeet_worker", "whistle_client", "parakeet_client", "transcription")
    )


def fake_tools(directory):
    """flatpak, python3 and uname that only record how smoke.sh called them."""
    directory.mkdir(parents=True)
    log = directory / "calls"
    tools = {
        "flatpak": 'printf "flatpak %s\\n" "$*" >> "$LOG"',
        "uname": "echo x86_64",
        "python3": "\n".join(
            [
                'printf "python3 %s\\n" "$*" >> "$LOG"',
                'if [ "$1" = tests/parakeet/prepare_model.py ]; then',
                '  mkdir -p "$3/parakeet-ultra-0.6b-int8/18cb623004dd6c15999b93cb70b14d8a1f6e8f3a2a97ff733f104bfc3494f6a9"',
                "fi",
            ]
        ),
    }
    for name, body in tools.items():
        (directory / name).write_text("#!/bin/sh\n" + body + "\n")
        (directory / name).chmod(0o755)
    return log


def smoke(tmp_path, *extra):
    bin_dir = tmp_path / "bin"
    log = fake_tools(bin_dir)
    environment = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LOG": str(log),
        "HOME": str(tmp_path),
        "CASSINI_PARAKEET_TEST_CACHE": str(tmp_path / "models"),
    }
    result = subprocess.run(
        ["sh", str(ROOT / "build-aux/flatpak/smoke.sh"), "app.flatpak", "io.example.App", *extra],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = (
        log.read_text().replace(str(ROOT), "$ROOT").replace(str(tmp_path), "$TMP").splitlines() if log.exists() else []
    )
    # A developer's checkout may hold a built portal already; that call is not under test.
    return result, [call for call in calls if "prepare_trash_portal.py" not in call]


def test_smoke_without_goldens_runs_every_check_but_the_golden_decodes(tmp_path):
    full, full_calls = smoke(tmp_path / "full")
    light, light_calls = smoke(tmp_path / "light", "--without-goldens")
    assert full.returncode == light.returncode == 0, (full.stderr, light.stderr)
    model = "$TMP/models/parakeet-ultra-0.6b-int8/18cb623004dd6c15999b93cb70b14d8a1f6e8f3a2a97ff733f104bfc3494f6a9"
    shared = [
        "flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo",
        "flatpak install --user -y --noninteractive --reinstall --bundle app.flatpak",
        "python3 tests/flatpak/library_persistence.py io.example.App --output $ROOT/flatpak-library-persistence.json",
        "python3 tests/flatpak/companion_bridge.py io.example.App --output $ROOT/flatpak-companion-bridge.json",
        "python3 tests/flatpak/trash.py io.example.App --output $ROOT/flatpak-trash.json"
        " --portal-prefix $ROOT/_ci-trash-portal",
    ]
    run = (
        "flatpak run --unshare=network --no-documents-portal --filesystem=$ROOT/build-aux:ro"
        " --filesystem=$ROOT/tests:ro"
    )
    smoke_installed = "--command=python3 io.example.App -I $ROOT/build-aux/smoke-installed.py /app/share/cassini-talk"
    assert full_calls == [
        *shared,
        "python3 tests/parakeet/prepare_model.py --cache $TMP/models",
        f"{run} --filesystem={model}:ro {smoke_installed} --whistle $ROOT/tests/whistle --parakeet {model}"
        " --parakeet-tests $ROOT/tests/parakeet",
    ]
    assert light_calls == [*shared, f"{run} {smoke_installed} --engines"]
    wrong, calls = smoke(tmp_path / "wrong", "--no-goldens")
    assert wrong.returncode == 2 and calls == []


@pytest.mark.parametrize(
    "arguments",
    [["--engines", "--whistle", "w"], ["--engines", "--parakeet", "m", "--parakeet-tests", "t"], ["--parakeet", "m"]],
)
def test_smoke_installed_refuses_mixed_or_incomplete_engine_options(arguments, tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "build-aux/smoke-installed.py"), str(tmp_path), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2 and "usage:" in result.stderr


# The devel bundle's python3-numpy stage comes from the flatpak-builder cache
# (G8) while the stable one is built fresh, so once the devel goldens are gone
# --engines must itself import, use and hash-check the bundle's NumPy.

SMOKE_INSTALLED = ROOT / "build-aux/smoke-installed.py"


def smoke_installed_functions(*names):
    tree = ast.parse(SMOKE_INSTALLED.read_text())
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert sorted(node.name for node in found) == sorted(names)
    namespace = {"Path": Path, "sys": sys}
    exec(compile(ast.Module(body=found, type_ignores=[]), str(SMOKE_INSTALLED), "exec"), namespace)
    return namespace


def test_the_engines_smoke_checks_numpy_in_the_installed_bundle():
    tree = ast.parse(SMOKE_INSTALLED.read_text())
    engines_only = [node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "args.engines"]
    calls = [ast.unparse(call.func) for node in engines_only for call in ast.walk(node) if isinstance(call, ast.Call)]
    assert "verify_numpy" in calls


def fake_wheel(site, name="cassini_fake"):
    package = site / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "core.so").write_bytes(b"\x7fELF fake")
    info = site / f"{name}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    rows = []
    for path in [package / "__init__.py", package / "core.so", info / "METADATA"]:
        digest = hashlib.sha256(path.read_bytes()).digest()
        rows.append(
            f"{path.relative_to(site).as_posix()},sha256={base64.urlsafe_b64encode(digest).rstrip(b'=').decode()},1"
        )
    # A console script the manifest's cleanup removes: outside site-packages.
    rows.append("../../../bin/cassini-fake,sha256=AAAA,1")
    # A static library the manifests' "*.a" cleanup deletes inside site-packages.
    rows.append(f"{name}/lib/libfake.a,sha256=AAAA,1")
    rows.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return package, info


@pytest.mark.parametrize("damage", [None, "byte", "missing", "no-record", "md5"])
def test_verify_record_checks_every_installed_file_of_a_wheel(damage, tmp_path, monkeypatch):
    site = tmp_path / "lib/python3/site-packages"
    package, info = fake_wheel(site)
    if damage == "byte":
        (package / "core.so").write_bytes(b"\x7fELF fakf")
    elif damage == "missing":
        (package / "core.so").unlink()
    elif damage == "no-record":
        (info / "RECORD").unlink()
    elif damage == "md5":
        record = (info / "RECORD").read_text().replace("cassini_fake/core.so,sha256=", "cassini_fake/core.so,md5=")
        (info / "RECORD").write_text(record)
    monkeypatch.syspath_prepend(str(site))
    verify_record = smoke_installed_functions("verify_record")["verify_record"]
    if damage is None:
        distribution = verify_record("cassini_fake")
        assert Path(distribution.locate_file("cassini_fake")) == package
    else:
        with pytest.raises(SystemExit, match="cassini_fake"):
            verify_record("cassini_fake")


def test_verify_record_excuses_exactly_the_manifests_unrooted_cleanup_globs():
    # The Flatpak build deletes these wherever they are (NumPy's libnpymath.a among
    # them), so a RECORD row they match is not a missing file; rooted patterns
    # (/include, /lib/pkgconfig) never reach site-packages.
    default = smoke_installed_functions("verify_record")["verify_record"].__defaults__[0]
    for name in ("io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"):
        manifest = json.loads((ROOT / f"build-aux/flatpak/{name}.json").read_text())
        unrooted = [glob for glob in manifest["cleanup"] if not glob.startswith("/")]
        assert sorted(unrooted) == sorted(default), name


def test_verify_numpy_imports_computes_and_matches_its_record(monkeypatch):
    numpy = pytest.importorskip("numpy")
    functions = smoke_installed_functions("verify_record", "verify_numpy")
    checked = []

    class Installed:
        def locate_file(self, name):
            return Path(numpy.__file__).parent.parent / name

    def record(name):
        checked.append(name)
        return Installed()

    functions["verify_record"] = record
    assert functions["verify_numpy"]() == numpy.__version__
    assert checked == ["numpy"]

    class Elsewhere:
        def locate_file(self, name):
            return Path("/nowhere") / name

    functions["verify_record"] = lambda name: Elsewhere()
    with pytest.raises(SystemExit, match="NumPy"):
        functions["verify_numpy"]()
