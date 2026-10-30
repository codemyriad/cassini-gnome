#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Flatpak EOL rebase: retain settings, voices, models and Library paths."""

import hashlib
import json
import os
import subprocess
import sys
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


def run(*args):
    return subprocess.check_output(args, text=True, timeout=180)


run("flatpak", "build-import-bundle", str(REPO), str(BUNDLE))
remote = "cassini-talk-upgrade-test"
run("flatpak", "remote-add", "--user", "--no-gpg-verify", remote, REPO.resolve().as_uri())
run("flatpak", "install", "--user", "--assumeyes", "--noninteractive", "--no-deps", remote, OLD)

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
subprocess.run(["sh", str(ROOT / "build-aux/flatpak/rename-ref.sh"), str(REPO)], check=True)
run("flatpak", "update", "--user", "--assumeyes", "--noninteractive", "--no-deps", OLD)
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
REPORT.write_text(json.dumps(dict(before=before, after=after, flatpakManagedRebase=True), indent=2) + "\n")
print("Flatpak's managed rebase retained the old data and absolute Library path")
