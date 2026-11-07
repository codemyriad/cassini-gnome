#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade a disposable legacy package with the actual newly built package.

Only package build containers run this: no user processes/data are consulted.
The fixture owns its legacy payload, broker and synthetic user files.
"""

import argparse
import hashlib
import io
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

LEGACY = "cassini-viewer"
CURRENT = "cassini-talk"
LEGACY_DIR = Path("/usr/share/cassini-viewer")
SHARED = ("/usr/bin/cassini-browser-host", "/usr/bin/cassini-browser-control-host")
OLD_MARKER = b"cassini legacy migration fixture"
BOUND = 15
BROKER = """import signal
signal.signal(signal.SIGINT, lambda *_: exit(0))
print("READY", flush=True)
while True:
    signal.pause()
"""


def command(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def legacy_tree(root):
    tree = root / "legacy"
    files = {
        "usr/bin/cassini-viewer": b"#!/bin/sh\n# " + OLD_MARKER + b"\nexit 0\n",
        **{name.lstrip("/"): b"#!/bin/sh\n# " + OLD_MARKER + b"\nexit 0\n" for name in SHARED},
        "usr/share/cassini-viewer/cassini_viewer/__init__.py": b"",
        "usr/share/cassini-viewer/cassini_viewer/browser_service.py": BROKER.encode(),
        "usr/share/cassini-viewer/legacy-marker": OLD_MARKER,
    }
    for name, content in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if name.startswith("usr/bin/") else 0o644)
    return tree


def legacy_package(kind, root):
    tree = legacy_tree(root)
    if kind == "deb":
        control = tree / "DEBIAN/control"
        control.parent.mkdir()
        control.write_text(
            "Package: cassini-viewer\nVersion: 0.3.0\nArchitecture: all\n"
            "Maintainer: Cassini migration test <test@example.invalid>\n"
            "Description: Disposable legacy Cassini package fixture\n"
        )
        package = root / "cassini-viewer_0.3.0_all.deb"
        command("dpkg-deb", "--build", "--root-owner-group", tree, package)
        return package
    if kind == "rpm":
        top = root / "rpm"
        top.mkdir()
        spec = root / "legacy.spec"
        spec.write_text(
            "Name: cassini-viewer\nVersion: 0.3.0\nRelease: 1\n"
            "Summary: Disposable legacy Cassini fixture\nLicense: GPL-3.0-or-later\n"
            "BuildArch: noarch\nAutoReqProv: no\n%description\nMigration test only.\n"
            '%install\nmkdir -p "%{buildroot}"\n'
            f'cp -a "{tree}/." "%{{buildroot}}/"\n'
            "%files\n/usr/bin/cassini-viewer\n/usr/bin/cassini-browser-host\n"
            "/usr/bin/cassini-browser-control-host\n/usr/share/cassini-viewer\n"
        )
        command("rpmbuild", "-bb", "--define", f"_topdir {top}", spec)
        (package,) = top.glob("RPMS/noarch/*.rpm")
        return package
    package = root / "cassini-viewer-0.3.0-1-any.pkg.tar"
    # Pacman's native package format: .PKGINFO plus the fixture's owned files.
    info = (
        b"pkgname = cassini-viewer\npkgbase = cassini-viewer\nxdata = pkgtype=pkg\npkgver = 0.3.0-1\n"
        b"pkgdesc = Disposable legacy Cassini fixture\narch = any\n"
        b"builddate = 1794000000\npackager = Cassini migration test\n"
        b"license = GPL-3.0-or-later\nsize = 1024\n"
    )
    with tarfile.open(package, "w") as archive:
        entry = tarfile.TarInfo(".PKGINFO")
        entry.size, entry.mode = len(info), 0o644
        archive.addfile(entry, io.BytesIO(info))
        for path in sorted(tree.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(tree)), recursive=False)
    return package


def installed(kind, name):
    args = {
        "deb": ["dpkg-query", "-W", "-f=${db:Status-Status}", name],
        "rpm": ["rpm", "-q", name],
        "arch": ["pacman", "-Q", name],
    }[kind]
    result = subprocess.run(args, capture_output=True, text=True)
    return result.returncode == 0 and (kind != "deb" or result.stdout == "installed")


def owner(kind, path):
    if kind == "deb":
        return output("dpkg-query", "-S", path).split(":", 1)[0]
    if kind == "rpm":
        return output("rpm", "-qf", "--qf", "%{NAME}", path)
    return output("pacman", "-Qqo", path)


def install_candidate(kind, candidate, root, env):
    if kind == "arch":
        # Exercise the repository rename path (Replaces), including Conflicts.
        # Only this local repo is configured: no unrelated system upgrades.
        repo = root / "repo"
        repo.mkdir()
        shutil.copyfile(candidate, repo / candidate.name)
        database = repo / "cassini-migration.db.tar.gz"
        command("repo-add", database, repo / candidate.name)
        config = root / "pacman.conf"
        config.write_text(
            f"[options]\nArchitecture = auto\n[cassini-migration]\nSigLevel = Never\nServer = file://{repo}\n"
        )
        args = ["pacman", "--config", str(config), "-Syu", "--noconfirm"]
    elif kind == "deb":
        args = ["apt-get", "install", "-y", "--no-install-recommends", str(candidate)]
    elif shutil.which("dnf"):
        args = ["dnf", "-y", "--setopt=install_weak_deps=False", "install", str(candidate)]
    else:
        args = ["zypper", "-n", "install", "--allow-unsigned-rpm", str(candidate)]
    # Retain the build's bounded dependency-install retries, never retry checks.
    retry = Path(__file__).with_name("retry.sh")
    command("sh", "-ec", '. "$1"; shift; retry "$@"', "migration-install", retry, *args, env=env)


def run(kind, candidate):
    if os.environ.get("CASSINI_TEST_PACKAGE_MIGRATION") != "1" or not any(
        Path(p).exists() for p in ("/.dockerenv", "/run/.containerenv")
    ):
        raise RuntimeError("Package migration smoke runs only in its disposable build container")
    if installed(kind, LEGACY) or installed(kind, CURRENT):
        raise RuntimeError("Package migration smoke requires a clean package state")
    with tempfile.TemporaryDirectory(prefix="cassini-package-migration-") as temporary:
        root = Path(temporary)
        legacy = legacy_package(kind, root)
        args = {"deb": ["dpkg", "-i"], "rpm": ["rpm", "-i", "--nosignature"], "arch": ["pacman", "-U", "--noconfirm"]}[
            kind
        ]
        command(*args, legacy)
        assert installed(kind, LEGACY)
        user = root / "user"
        saved = {}
        for name in (
            "Music/Cassini/Recording-fixture/audio.wav",
            ".config/cassini-viewer/settings.json",
            ".local/share/cassini/voices/private.bin",
        ):
            path = user / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"Generated user-owned data; package upgrades must leave this intact\x00")
            path.chmod(0o600)
            saved[path] = hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mode
        env = dict(
            os.environ, HOME=str(user), XDG_CONFIG_HOME=str(user / ".config"), XDG_DATA_HOME=str(user / ".local/share")
        )
        with subprocess.Popen(
            [sys.executable, "-m", "cassini_viewer.browser_service", "--serve", str(root / "legacy.sock")],
            # Like an unprivileged desktop user, do not add unowned bytecode
            # files to the root-owned legacy package payload.
            env=dict(env, PYTHONPATH=str(LEGACY_DIR), PYTHONDONTWRITEBYTECODE="1"),
            stdout=subprocess.PIPE,
            text=True,
        ) as broker:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(broker.stdout, selectors.EVENT_READ)
                    assert selector.select(BOUND), "legacy fixture did not become ready"
                assert broker.stdout.readline().strip() == "READY"
                install_candidate(kind, candidate, root, env)
                assert installed(kind, CURRENT) and not installed(kind, LEGACY)
                assert not LEGACY_DIR.exists() and not Path("/usr/bin/cassini-viewer").exists()
                for path in SHARED:
                    assert owner(kind, path) == CURRENT
                    assert OLD_MARKER not in Path(path).read_bytes()
                assert broker.poll() is None, "package installation killed a running legacy broker"
                assert all(
                    (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mode) == fact for p, fact in saved.items()
                )
                print(
                    f"PASS {kind}: legacy files removed, current hosts owned, running broker and user data preserved",
                    flush=True,
                )
            finally:
                if broker.poll() is None:
                    broker.send_signal(signal.SIGINT)
                try:
                    broker.communicate(timeout=BOUND)
                except subprocess.TimeoutExpired:
                    broker.kill()  # Only this smoke test's own fixture process.
                    broker.communicate(timeout=BOUND)
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("deb", "rpm", "arch"))
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    run(args.kind, args.candidate.resolve(strict=True))


if __name__ == "__main__":
    main()
