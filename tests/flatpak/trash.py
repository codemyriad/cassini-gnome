#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed sandbox: Move to Trash, Undo/Restore and purge stay inside the Library (D100, D-1032).

The meeting moves into `<Library>/.cassini/trash/<entry-id>/` with no extra grants; the real
desktop Trash portal is running, and nothing may reach the host Trash (S-12 §7.1).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def worker(application, private, output, portal_prefix=None):
    assert str(private).startswith("/tmp/cassini-flatpak-trash-")
    assert Path(os.environ["HOME"]) == private / "home"
    portal = (
        portal_prefix / "libexec/xdg-desktop-portal"
        if portal_prefix
        else next(
            (Path(p) for p in ("/usr/libexec/xdg-desktop-portal", "/usr/lib/xdg-desktop-portal") if Path(p).is_file()),
            None,
        )
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
    prefix = "import sys;sys.path.insert(0,'/app/share/cassini-talk');"

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
                "import json;from cassini_talk.recording_session import RecordingSession;"
                "s=RecordingSession(None,title='Private native Trash check',source='gnome');"
                "s.add_track('mic','Me','microphone',48000,1);"
                "s.append('mic',b'\\x01\\x00'*9600,0);s.finish();"
                "from cassini_talk.library_publication import publish_stopped;"
                "from cassini_talk.library_location import primary;"
                "result=publish_stopped(s);"
                "print(json.dumps(dict(path=str(s.directory),file=str(primary()/result.name))))"
            )
            folder = Path(created["path"])
            music = private / "Musica" / "Cassini"
            assert folder.parent == music / ".cassini/sessions" and folder.is_dir(), (folder, music)
            published = Path(created["file"])
            assert published.parent == music and published.is_file()
            audio_hash = hashlib.sha256(published.read_bytes()).hexdigest()
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
            scan = (
                "import json;from cassini_talk.library import LibraryCollectionScanner;"
                "ms=LibraryCollectionScanner().scan().meetings;"
            )
            result = inside(
                "import gi;gi.require_version('Adw','1');"
                + scan
                + "from cassini_talk.meeting_trash import trash_meeting;"
                "(m,)=ms;r,e=trash_meeting(m);print(json.dumps(dict(root=str(r),entry=e)))"
            )
            entry = music / ".cassini" / "trash" / result["entry"]
            assert Path(result["root"]) == music and not folder.exists() and not published.exists()
            assert hashlib.sha256((entry / "meeting.opus").read_bytes()).hexdigest() == audio_hash
            moved = entry / folder.name
            assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in moved.iterdir()} == before
            host_trash = private / "data" / "Trash"
            assert not host_trash.exists() or not any(host_trash.rglob("*")), "nothing may reach the host Trash"
            hidden = inside(scan + "print(json.dumps(dict(meetings=len(ms))))")
            assert hidden["meetings"] == 0
            restored = inside(
                "import json;from pathlib import Path;from cassini_talk.library_v2 import trash;"
                f"print(json.dumps(dict(name=trash.restore(Path({str(music)!r}),{result['entry']!r}))))"
            )
            assert restored["name"] == published.name and not entry.exists()
            assert hashlib.sha256(published.read_bytes()).hexdigest() == audio_hash
            assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()} == before
            assert inside(scan + "print(json.dumps(dict(meetings=len(ms))))")["meetings"] == 1
            purged = inside(
                "import gi;gi.require_version('Adw','1');"
                + scan
                + "from cassini_talk.meeting_trash import trash_meeting;"
                "from cassini_talk.library_v2 import trash;(m,)=ms;r,e=trash_meeting(m);trash.purge(r,e);"
                "print(json.dumps(dict(left=len(trash.entries(r)))))"
            )
            assert purged["left"] == 0 and not folder.exists() and not published.exists()
            assert not any((music / ".cassini" / "trash").iterdir())
            assert not host_trash.exists() or not any(host_trash.rglob("*"))
            restarted = inside(scan + "print(json.dumps(dict(meetings=len(ms))))")
            assert restarted["meetings"] == 0
            output.write_text(
                json.dumps(
                    dict(
                        application=application,
                        installed=True,
                        noExtraGrants=True,
                        libraryTrash=True,
                        hostTrashUntouched=True,
                        allBytesPreserved=True,
                        restoredByteIdentical=True,
                        purgeRemovedEverything=True,
                        restartStillRemoved=True,
                        files=before,
                        publishedAudioSHA256=audio_hash,
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
    parser.add_argument("--portal-prefix", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args.application, args.worker, output, args.portal_prefix)
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
        if args.portal_prefix:
            prefix = args.portal_prefix.resolve()
            environment["XDG_DATA_DIRS"] = (
                str(prefix / "share") + ":" + environment.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
            )
        for key in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "CASSINI_LIBRARY_DIR"):
            environment.pop(key, None)
        portal_options = ["--portal-prefix", str(args.portal_prefix.resolve())] if args.portal_prefix else []
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
                *portal_options,
            ],
            env=environment,
            check=True,
            timeout=100,
        )
    print(output.read_text())


if __name__ == "__main__":
    main()
