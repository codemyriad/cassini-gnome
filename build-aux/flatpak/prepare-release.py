#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare D45.1 signed local artifacts; never upload or read/export private keys."""

import argparse
import base64
import os
import re
import subprocess
import tempfile
from pathlib import Path

APP = "io.codemyriad.CassiniTalk"
OLD = "com.gocassini.Viewer"
URL = "https://cassini.talk/download/linux/flatpak/"


def prepare(repo, output, key, homedir, *, legacy_bundle=None):
    repo, output, homedir = (Path(p).resolve() for p in (repo, output, homedir))
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", key):
        raise ValueError("Use the full signing-key fingerprint")
    if any(homedir == p or homedir.is_relative_to(p) or p.is_relative_to(homedir) for p in (repo, output)):
        raise ValueError("The signing key must be outside repository/output directories")
    if not homedir.is_dir():
        raise ValueError("The signing key directory does not exist")

    def call(*args):
        return subprocess.check_output(args, timeout=300)

    # Export only the public key; gpg/Flatpak use the private key in place.
    public = call("gpg", "--batch", "--homedir", str(homedir), "--export", key)
    if not public:
        raise ValueError("The signing key has no exportable public key")
    if legacy_bundle is not None:
        call("flatpak", "build-import-bundle", str(repo), str(Path(legacy_bundle).resolve()))
    refs = [line.split()[0] for line in call("flatpak", "repo", "--branches", str(repo)).decode().splitlines()]
    allowed = {APP, OLD, APP + ".Locale", OLD + ".Locale"}
    for ref in refs:
        parts = ref.split("/")
        if len(parts) == 4 and parts[0] in ("app", "runtime") and parts[1] not in allowed:
            raise ValueError("Use a stable-only release repository; unexpected ref: " + ref)
    arch = call("flatpak", "--default-arch").decode().strip()
    for app in (APP, OLD):
        if f"app/{app}/{arch}/master" not in refs:
            raise ValueError(f"Required release ref is absent: {app}")
    call("sh", str(Path(__file__).with_name("rename-ref.sh")), str(repo))
    signing = ["--gpg-homedir=" + str(homedir), "--gpg-sign=" + key]
    for ref in refs:
        parts = ref.split("/")
        if len(parts) != 4 or parts[0] not in ("app", "runtime"):
            continue
        kind, app, ref_arch, branch = parts
        if app not in (APP, OLD, APP + ".Locale", OLD + ".Locale"):
            continue
        call(
            "flatpak",
            "build-sign",
            *signing,
            "--arch=" + ref_arch,
            *(["--runtime"] if kind == "runtime" else []),
            str(repo),
            app,
            branch,
        )
    call("flatpak", "build-update-repo", *signing, "--title=Cassini talk", "--default-branch=master", str(repo))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cassini-release-", dir=output) as temporary:
        staging = Path(temporary)
        public_file = staging / "cassini-talk.gpg"
        public_file.write_bytes(public)
        (staging / "cassini-talk.flatpakrepo").write_text(
            "[Flatpak Repo]\nTitle=Cassini talk\nUrl=" + URL + "\nHomepage=https://cassini.talk/\n"
            "DefaultBranch=master\nGPGKey=" + base64.b64encode(public).decode() + "\n"
        )
        call(
            "flatpak",
            "build-bundle",
            "--repo-url=" + URL,
            "--gpg-keys=" + str(public_file),
            "--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo",
            str(repo),
            str(staging / "cassini-talk.flatpak"),
            APP,
        )
        for name in ("cassini-talk.gpg", "cassini-talk.flatpakrepo", "cassini-talk.flatpak"):
            os.replace(staging / name, output / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--key", required=True, help="full public signing-key fingerprint")
    parser.add_argument("--gpg-homedir", type=Path, required=True)
    parser.add_argument("--legacy-bundle", type=Path, help="released old-ID bundle, if not already in repo")
    args = parser.parse_args()
    try:
        prepare(args.repo, args.output, args.key, args.gpg_homedir, legacy_bundle=args.legacy_bundle)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Could not prepare the signed Flatpak release: {error}\n")
    print("Signed Flatpak repository, repo file and bundle prepared locally; nothing published")


if __name__ == "__main__":
    main()
