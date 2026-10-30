#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Flatpak EOL rebase: retain settings, voices, models and Library paths."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OLD = "com.gocassini.Viewer"
NEW = "io.codemyriad.CassiniTalk"
ROOT = Path(__file__).resolve().parents[2]
REPO, BUNDLE, REPORT = map(Path, sys.argv[1:])
assert os.environ.get("CASSINI_QA_RENAME") == "1", "Run only in the isolated packaging gate"
for app in (OLD, NEW):
    assert not (Path.home() / ".var/app" / app).exists(), "Use a fresh private Flatpak test home"
assert (
    hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    == "b27ffe4f9ba3815e0e74f6d93e94e740bda10b4ef0b7aa6b348e8392f835507c"
)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=180, **kwargs)


run("flatpak", "install", "--user", "--assumeyes", "--noninteractive", "--no-deps", "--bundle", str(BUNDLE))

# The legacy app writes its own keyfile and private data in its real sandbox.
seed = r"""
import hashlib, json, os
from pathlib import Path
from gi.repository import Gio
s = Gio.Settings.new("com.gocassini.Viewer")
s.set_int("window-width", 935)
s.set_string("speech-model", "parakeet-ultra-0.6b-int8")
Gio.Settings.sync()
data = Path(os.environ["XDG_DATA_HOME"]) / "cassini"
config = Path(os.environ["XDG_CONFIG_HOME"]) / "cassini/library.json"
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps(dict(version=1, primary=str(data / "Library"), pending=[], aliases={})))
files = {}
for name in ("Library/upgrade-sentinel.bin", "models/upgrade-sentinel.bin", "voices/upgrade-sentinel.bin"):
    p = data / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((name + "\n").encode())
    files[name] = hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(dict(files=files, primary=str(data / "Library"))))
"""
before = json.loads(run("flatpak", "run", "--no-documents-portal", "--command=python3", OLD, "-c", seed))
with tempfile.TemporaryDirectory(prefix="cassini-signing-test-", dir=REPORT.parent) as temporary:
    work = Path(temporary)
    homedir = work / "keyring"
    homedir.mkdir(mode=0o700)
    run("gpg", "--batch", "--homedir", str(homedir), "--pinentry-mode", "loopback", "--passphrase", "",
        "--quick-generate-key", "Cassini temporary signing test", "rsa2048", "sign", "1d")
    listing = run("gpg", "--batch", "--homedir", str(homedir), "--with-colons", "--list-keys")
    key = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    output = work / "artifacts"
    run(sys.executable, str(ROOT / "build-aux/flatpak/prepare-release.py"), str(REPO), str(output),
        "--key", key, "--gpg-homedir", str(homedir), "--legacy-bundle", str(BUNDLE))
    # The test uses a local URL but the production artifact must name D45.1's
    # canonical origin. Signature verification is enabled, never bypassed.
    repo_file = output / "cassini-talk.flatpakrepo"
    description = repo_file.read_text()
    canonical = "https://cassini.talk/download/linux/flatpak/"
    assert "Url=" + canonical + "\n" in description and "GPGKey=" in description
    local = work / "local.flatpakrepo"
    local.write_text(description.replace("Url=" + canonical, "Url=" + REPO.resolve().as_uri()))
    remote = "cassini-talk-upgrade-test"
    run("flatpak", "remote-add", "--user", "--if-not-exists", "--from", remote, str(local))
    run("flatpak", "install", "--user", "--assumeyes", "--noninteractive", "--no-deps", "--reinstall", remote, OLD)
    # Installing a bundle in a separate private installation must configure
    # its canonical update origin even with no network or runtime downloads.
    bundle_env = dict(os.environ, FLATPAK_USER_DIR=str(work / "bundle-installation"))
    run("flatpak", "install", "--user", "--assumeyes", "--noninteractive", "--no-deps", "--bundle",
        str(output / "cassini-talk.flatpak"), env=bundle_env)
    origin = run("flatpak", "info", "--user", "--show-origin", NEW, env=bundle_env).strip()
    remotes = run("flatpak", "remotes", "--user", "--columns=name,url", env=bundle_env)
    assert any(line.split() == [origin, canonical] for line in remotes.splitlines()), remotes

apps = run("flatpak", "list", "--user", "--app", "--columns=application").splitlines()
assert NEW in apps and OLD not in apps, apps

probe = r"""
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, "/app/share/cassini-viewer")
from cassini_viewer.identity import settings
from cassini_viewer.library_location import load
s = settings()
assert s.get_int("window-width") == 935
assert s.get_string("speech-model") == "parakeet-ultra-0.6b-int8"
assert s.get_boolean("legacy-settings-migrated")
data = Path(os.environ["XDG_DATA_HOME"]) / "cassini"
files = {name: hashlib.sha256((data / name).read_bytes()).hexdigest() for name in (
    "Library/upgrade-sentinel.bin", "models/upgrade-sentinel.bin", "voices/upgrade-sentinel.bin")}
library = load()["primary"]
assert (Path(library) / "upgrade-sentinel.bin").is_file(), "Old absolute Library path must still resolve"
print(json.dumps(dict(files=files, primary=library, settingsMigrated=True)))
"""
after = json.loads(run("flatpak", "run", "--no-documents-portal", "--command=python3", NEW, "-c", probe))
assert before["files"] == after["files"] and before["primary"] == after["primary"]
old_dir = Path.home() / ".var/app" / OLD
new_dir = Path.home() / ".var/app" / NEW
assert old_dir.is_symlink() and old_dir.resolve() == new_dir.resolve()
REPORT.write_text(json.dumps(dict(before=before, after=after, flatpakManagedRebase=True,
    signedRepository=True, bundleOrigin=canonical), indent=2) + "\n")
print("Flatpak's managed rebase retained the old data and absolute Library path")
