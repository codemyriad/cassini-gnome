#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real sandbox restarts retain PCM with localized, absent, disabled or revoked Music."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("application")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--modules", type=Path, default=Path("/app/share/cassini-viewer"))
parser.add_argument(
    "--grant-for-local-proof",
    action="store_true",
    help="Test an older local installation with the proposed scope; never used by package CI",
)
args = parser.parse_args()
permissions = subprocess.check_output(["flatpak", "info", "--user", "--show-permissions", args.application], text=True)
assert "filesystems=home" not in permissions and ";home" not in permissions
scoped = "xdg-music/Cassini:create" in permissions
assert scoped or args.grant_for_local_proof, "The installed manifest must declare the scoped persistent Library"
location = subprocess.check_output(
    ["flatpak", "info", "--user", "--show-location", args.application], text=True
).strip()
repository = location.split("/app/")[0]
assert repository != location
inside = """import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cassini_viewer.library_location import primary
from cassini_viewer.recording_session import RecordingSession
expected=Path(sys.argv[2]);root=primary()
exports=Path(sys.argv[4])
assert root == expected,(root,expected)
if sys.argv[3] == "write":
    session=RecordingSession(None,source="gnome",title="Private persistence check")
    for identifier,kind,data in (("mic","microphone",b"\\x01\\x00"),("system","system",b"\\x02\\x00")):
        session.add_track(identifier,identifier,kind,48000,1)
        session.append(identifier,data*9600,0)
    session.finish()
else:
    assert len(list(root.glob(".cassini/sessions/Recording-*"))) == 1
folder,=root.glob(".cassini/sessions/Recording-*")
manifest=json.loads((folder/"session.json").read_text())
assert manifest["state"] == "completed"
assert {t["kind"] for t in manifest["tracks"]} == {"microphone","system"}
assert all(t["frames"] == 9600 for t in manifest["tracks"])
from cassini_viewer.exporting import export_meeting
from cassini_viewer.open_meeting import load_open_meeting
from cassini_viewer import portable
if sys.argv[3] == "write":
    meeting=load_open_meeting(folder/"session.json")
    unsafe=Path.home()/"Documents/Cassini-temporary-export"
    try: export_meeting(meeting,unsafe,"markdown")
    except PermissionError: pass
    else: raise AssertionError("Export accepted Flatpak's temporary private home")
    assert not unsafe.exists()
    export_meeting(meeting,exports,"markdown")
    export_meeting(meeting,exports,"cassini")
files=sorted(exports.iterdir())
assert {p.suffix for p in files} == {".md",".opus"}
opus=next(p for p in files if p.suffix == ".opus")
assert portable.verify_audio(str(opus),portable.read_meeting(str(opus))).state == portable.State.OK
print(json.dumps({"library":str(root),"folder":str(folder.relative_to(root)),"sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()},"exports":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}))
"""
results = []
for mode in ("localized-music", "unconfigured-music", "disabled-music", "revoked-music"):
    # Keep HOME under home: Flatpak may bind /var/tmp, which masks this bug.
    with tempfile.TemporaryDirectory(prefix="cassini-flatpak-persistence-", dir=Path.home()) as temporary:
        private = Path(temporary)
        config, home, music, exports = (private / name for name in ("config", "home", "Musica", "Exports"))
        for directory in (config, home, music, exports):
            directory.mkdir()
        if mode != "unconfigured-music":
            configured = "$HOME" if mode == "disabled-music" else str(music)
            (config / "user-dirs.dirs").write_text(f'XDG_MUSIC_DIR="{configured}"\n')
        env = dict(
            os.environ,
            HOME=str(home),
            XDG_CONFIG_HOME=str(config),
            XDG_DATA_HOME=str(private / "data"),
            XDG_CACHE_HOME=str(private / "cache"),
            FLATPAK_USER_DIR=repository,
        )
        env.pop("CASSINI_LIBRARY_DIR", None)
        command = [
            "flatpak",
            "run",
            "--user",
            "--command=python3",
            "--unshare=network",
            "--unshare=ipc",
            "--nosocket=wayland",
            "--nosocket=x11",
            "--nosocket=fallback-x11",
            "--nosocket=pulseaudio",
            "--no-documents-portal",
        ]
        # A narrow per-launch fixture grant models only the folder the person
        # selected. The native FileDialog/portal call is covered by the GUI test.
        command.append(f"--filesystem={exports}:create")
        if args.grant_for_local_proof:
            command.append("--filesystem=xdg-music/Cassini:create")
        if mode == "revoked-music":
            command.append("--nofilesystem=xdg-music/Cassini")
        if args.modules != Path("/app/share/cassini-viewer"):
            command.append(f"--filesystem={args.modules.resolve()}:ro")
        expected = (
            music / "Cassini"
            if mode == "localized-music"
            else home / ".var/app" / args.application / "data/cassini/Library"
        )
        command += [args.application, "-I", "-c", inside, str(args.modules), str(expected)]
        reports = [
            json.loads(subprocess.check_output(command + ["write", str(exports)], env=env, text=True, timeout=20))
        ]
        if mode == "unconfigured-music":
            # Installing/configuring a Music folder later must not hide the
            # recordings already kept in the app-data Library.
            (config / "user-dirs.dirs").write_text(f'XDG_MUSIC_DIR="{music}"\n')
        reports.append(
            json.loads(subprocess.check_output(command + ["read", str(exports)], env=env, text=True, timeout=20))
        )
        assert reports[0] == reports[1], "Restart lost or changed the Library's PCM/manifest bytes"
        assert (expected / reports[0]["folder"] / "session.json").is_file()
        # HOME remains a private fixture. The real user's Library is never opened.
        results.append(
            dict(
                mode=mode,
                separateLaunches=2,
                pcmAndManifestPreserved=True,
                exportsPreserved=True,
                privateHomeExportRejected=True,
                selectedFolderFixtureGrant=True,
                **reports[1],
            )
        )
result = dict(
    installedManifestScope=scoped,
    installedApplicationModules=args.modules == Path("/app/share/cassini-viewer"),
    developmentGrant=args.grant_for_local_proof,
    cases=results,
)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
