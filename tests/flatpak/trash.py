#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed sandbox -> real desktop Trash portal -> private host Trash, no extra grants."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def worker(application, private, output):
    assert str(private).startswith("/tmp/cassini-flatpak-trash-")
    assert Path(os.environ["HOME"]) == private / "home"
    portal = next(
        (Path(p) for p in ("/usr/libexec/xdg-desktop-portal", "/usr/lib/xdg-desktop-portal") if Path(p).is_file()), None
    )
    assert portal is not None, "Installed Flatpak Trash proof needs the real xdg-desktop-portal host service"
    log = output.with_name("flatpak-trash-portal.log")
    base = [
        "flatpak",
        "run",
        "--user",
        "--unshare=network",
        "--unshare=ipc",
        "--nosocket=wayland",
        "--nosocket=x11",
        "--nosocket=fallback-x11",
        "--nosocket=pulseaudio",
        "--no-documents-portal",
        "--command=python3",
        application,
        "-I",
        "-c",
    ]
    prefix = "import sys;sys.path.insert(0,'/app/share/cassini-viewer');"

    def inside(code):
        result = subprocess.run(base + [prefix + code], capture_output=True, text=True, timeout=30)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
        result.check_returncode()
        return json.loads(result.stdout)

    with log.open("w") as stream:
        service = subprocess.Popen([str(portal), "--replace"], stdout=stream, stderr=subprocess.STDOUT)
        try:
            # GDBus activation/registration is bounded; no guessed startup sleep.
            subprocess.run(
                ["gdbus", "wait", "--session", "--timeout", "15", "org.freedesktop.portal.Desktop"],
                check=True,
                timeout=20,
            )
            created = inside(
                "import json;from cassini_viewer.recording_session import RecordingSession;"
                "s=RecordingSession(None,title='Private native Trash check',source='gnome');"
                "s.add_track('mic','Me','microphone',48000,1);"
                "s.append('mic',b'\\x01\\x00'*9600,0);s.finish();"
                "print(json.dumps(dict(path=str(s.directory))))"
            )
            folder = Path(created["path"])
            music = private / "Musica" / "Cassini"
            assert folder.parent == music and folder.is_dir(), (folder, music)
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
            result = inside(
                "import json,gi;from pathlib import Path;gi.require_version('Adw','1');"
                "from cassini_viewer.meeting_trash import trash_owned;"
                "from cassini_viewer.trash_cleanup import TrashCleanup;"
                "from cassini_viewer.voice_store import VoiceStore;"
                f"p=Path({str(folder)!r});trash_owned(p,cleanup=TrashCleanup(VoiceStore()));"
                "print(json.dumps(dict(removed=not p.exists())))"
            )
            assert result["removed"] and not folder.exists()
            host_trash = private / "data" / "Trash" / "files"
            (trashed,) = [p for p in host_trash.iterdir() if p.name.startswith(folder.name)]
            assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in trashed.iterdir()} == before
            restarted = inside(
                "import json;from cassini_viewer.library import LibraryCollectionScanner;"
                "print(json.dumps(dict(meetings=len(LibraryCollectionScanner().scan().meetings))))"
            )
            assert restarted["meetings"] == 0
            output.write_text(
                json.dumps(
                    dict(
                        application=application,
                        installed=True,
                        noExtraGrants=True,
                        nativeGioTrash=True,
                        hostTrash=True,
                        allBytesPreserved=True,
                        restartStillRemoved=True,
                        files=before,
                    ),
                    indent=2,
                )
                + "\n"
            )
        finally:
            service.terminate()
            try:
                service.wait(5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args.application, args.worker, output)
        return
    location = subprocess.check_output(
        ["flatpak", "info", "--user", "--show-location", args.application], text=True
    ).strip()
    repository = location.split("/app/")[0]
    assert repository != location
    with tempfile.TemporaryDirectory(prefix="cassini-flatpak-trash-") as temporary:
        private = Path(temporary)
        for name in ("home", "config", "data", "cache", "run", "Musica"):
            (private / name).mkdir(mode=0o700)
        (private / "config" / "user-dirs.dirs").write_text(f'XDG_MUSIC_DIR="{private}/Musica"\n')
        environment = dict(
            os.environ,
            HOME=str(private / "home"),
            XDG_CONFIG_HOME=str(private / "config"),
            XDG_DATA_HOME=str(private / "data"),
            XDG_CACHE_HOME=str(private / "cache"),
            XDG_RUNTIME_DIR=str(private / "run"),
            FLATPAK_USER_DIR=repository,
        )
        for key in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "CASSINI_LIBRARY_DIR"):
            environment.pop(key, None)
        subprocess.run(
            [
                "dbus-run-session",
                "--dbus-daemon=" + (shutil.which("dbus-daemon") or "dbus-daemon"),
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                args.application,
                "--output",
                str(output),
                "--worker",
                str(private),
            ],
            env=environment,
            check=True,
            timeout=100,
        )
    print(output.read_text())


if __name__ == "__main__":
    main()
