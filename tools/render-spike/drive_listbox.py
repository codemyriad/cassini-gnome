#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive one already-owned desktop using file/input/frame completion events."""

import argparse
import ctypes
import json
import os
import shlex
import subprocess
from pathlib import Path


def wait_for(path, predicate=None):
    """Linux file-event gate. The caller supplies the outer hang watchdog."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    try:
        watch = libc.inotify_add_watch(fd, os.fsencode(path.parent), 0x8 | 0x80 | 0x100)
        if watch < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch")
        while True:
            try:
                if path.exists():
                    json.loads(path.read_text())
                    if predicate is None or predicate():
                        return
            except (json.JSONDecodeError, FileNotFoundError):
                pass
            os.read(fd, 65536)
    finally:
        os.close(fd)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--qa", type=Path, required=True)
parser.add_argument("--case", required=True)
parser.add_argument("--fixture", default="fixture-60")
parser.add_argument("--mode", choices=("listbox", "listview"), default="listbox")
parser.add_argument("--container", choices=("listbox", "box"), default="listbox")
parser.add_argument("--fast-measure", choices=("0", "1"), default="1")
args = parser.parse_args()
shared = args.qa / "shared"
case = shared / args.case
case.mkdir(exist_ok=True)
if (case / "ready.json").exists():
    raise SystemExit("Use a fresh case directory; never accept stale readiness")


def desktop(command):
    return subprocess.run(
        [str(args.qa / "desktop.sh"), "run", command], text=True, capture_output=True, check=True, timeout=60
    ).stdout


def shell(code):
    command = (
        "gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "
        + shlex.quote(code)
    )
    result = desktop(command)
    if "(false," in result:
        raise RuntimeError(result)
    return result


def command(message):
    ack = case / "last-command.json"
    before = ack.stat().st_mtime_ns if ack.exists() else None
    with (case / "control").open("w") as pipe:
        pipe.write(message + "\n")
    wait_for(ack, lambda: ack.stat().st_mtime_ns != before and json.loads(ack.read_text())["command"] == message)


launch = "python3 /mnt/shared/app/tools/render-spike/run_listbox.py "
launch += " ".join(
    shlex.quote(x)
    for x in [
        "--fixture",
        f"/mnt/shared/{args.fixture}/synthetic.opus",
        "--output",
        f"/mnt/shared/{args.case}",
        "--mode",
        args.mode,
        "--fast-measure",
        args.fast_measure,
        "--container",
        args.container,
    ]
)
print(desktop(launch + f" > /mnt/shared/{args.case}.log 2>&1 & echo $!"), flush=True)
wait_for(case / "ready.json")
ready = json.loads((case / "ready.json").read_text())
print("READY", ready, flush=True)
shell(
    "(()=>{let w=global.get_window_actors().find(a=>a.meta_window.get_gtk_application_id()==="
    + json.dumps(ready["appId"])
    + ");if(w)w.meta_window.activate(global.get_current_time());return !!w;})()"
)


def settle(filename):
    command("settle " + filename)
    wait_for(case / filename)


# A fresh virtual device/seat needs one native main-loop turn before its
# first warp is usable; initialize separately from the first finger sequence.
shell(
    "(()=>{const C=imports.gi.Clutter;global._renderSpikePointer=C.get_default_backend().get_default_seat().create_virtual_device(C.InputDeviceType.POINTER_DEVICE);global._renderSpikePointer.notify_absolute_motion(imports.gi.GLib.get_monotonic_time(),900,450);return 'pointer warmup';})()"
)
print("POINTER", shell("JSON.stringify(global.get_pointer())"), flush=True)

for phase in ("paused", "playback"):
    command("pause" if phase == "paused" else "play")
    command("reset")
    settle(phase + "-reset.json")
    command("phase " + phase)
    script = (Path(__file__).with_name("flick.js")).read_text()
    destination = f"/mnt/shared/{args.case}/{phase}-input.json"
    write = (
        "imports.gi.Gio.File.new_for_path("
        + json.dumps(destination)
        + ").replace_contents(JSON.stringify(global._renderSpikeInput),null,false,imports.gi.Gio.FileCreateFlags.REPLACE_DESTINATION,null);"
    )
    script = script.replace("global._renderSpikeDone=true;", "global._renderSpikeDone=true;" + write)
    shell(script)
    wait_for(case / (phase + "-input.json"))
    settle(phase + "-coast-complete.json")
    command("dump " + phase + ".json")
    data = json.loads((case / (phase + ".json")).read_text())
    frames = [frame for frame in data["frames"] if frame["phase"] == phase]
    received = [event for event in data["input"] if frames[0]["us"] <= event["us"] <= frames[-1]["us"]]
    assert any(event["dy"] > 0 for event in received) and any(event["dy"] < 0 for event in received), (
        "Sent input never reached the app in both directions"
    )
    assert max(frame["value"] for frame in frames) > min(frame["value"] for frame in frames), (
        "No actual scroll movement"
    )
    print("DONE", args.case, phase, flush=True)
command("pause")
command("resize 1000")
settle("resize-narrow.json")
command("dump resize-narrow-result.json")
command("resize 1280")
settle("resize-wide.json")
command("dump resize-wide-result.json")
command("phase follow")
command("play")
command("follow")
settle("follow-position.json")
command("dump follow-result.json")
command("pause")
command("features")
wait_for(case / "features.json")
command("quit")
print("COMPLETE", args.case, flush=True)
