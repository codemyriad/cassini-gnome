#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D41 installed-package journeys on a fresh private GNOME 50 desktop.

Host SetStatus rejection is reported as an explicit platform limitation; it is
never substituted with a fake Background Apps entry or a tray implementation.
"""

import json
import math
import os
import signal
import subprocess
import time
import wave
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

assert os.environ.get("CASSINI_QA_DESKTOP") == "1" and not os.environ.get("DISPLAY")
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
OUT = Path("/out")
HOME = Path.home()


def until(check, label, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        try:
            value = check()
            if value:
                return value
        except (GLib.Error, OSError) as error:
            last = error
        time.sleep(0.05)
    raise AssertionError(f"Timed out: {label}; last error={last}")


def call(dest, path, iface, method, params=None):
    return bus.call_sync(dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()


def shell(code):
    ok, value = call("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval", GLib.Variant("(s)", (code,)))
    assert ok, value
    return json.loads(value) if value else None


def windows(appid):
    return shell(
        f"imports.gi.Shell.AppSystem.get_default().lookup_app({json.dumps(appid + '.desktop')})?.get_windows().length ?? 0"
    )


def owner(appid):
    return call(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (appid,)),
    )[0]


def pid(appid):
    return call(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetConnectionUnixProcessID",
        GLib.Variant("(s)", (appid,)),
    )[0]


def action(appid, name):
    return call(
        appid, "/" + appid.replace(".", "/"), "org.gtk.Actions", "Activate", GLib.Variant("(sava{sv})", (name, [], {}))
    )


def close_window(appid):
    shell(
        f"imports.gi.Shell.AppSystem.get_default().lookup_app({json.dumps(appid + '.desktop')}).get_windows()[0].delete(global.get_current_time()); true"
    )


def walk(node):
    yield node
    for index in range(node.get_child_count()):
        child = node.get_child_at_index(index)
        if child:
            yield from walk(child)


def click(label, *, dialog=None):
    def find():
        for node in walk(Atspi.get_desktop(0)):
            if (node.get_name() or "") != label:
                continue
            if not node.get_state_set().contains(Atspi.StateType.SHOWING):
                continue
            if dialog:
                parent = node.get_parent()
                while parent and not (parent.get_name() == dialog and parent.get_role_name() == "dialog"):
                    parent = parent.get_parent()
                if parent is None:
                    continue
            target = node
            while target:
                iface = target.get_action_iface()
                if iface and iface.get_n_actions():
                    assert iface.do_action(0)
                    return True
                target = target.get_parent()
        return False

    until(find, "click " + label)


def portal_rows(appid):
    rows = call(
        "org.freedesktop.background.Monitor",
        "/org/freedesktop/background/monitor",
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", ("org.freedesktop.background.Monitor", "BackgroundApps")),
    )[0]
    return [row for row in rows if row.get("app_id") == appid]


def listed(appid):
    return next(iter(portal_rows(appid)), None)


def shell_entries(appid):
    """What the person sees: GNOME Shell's Background Apps menu items for the app."""
    return shell(
        "Main.panel.statusArea.quickSettings._backgroundApps.quickSettingsItems[0]._appsSection._getMenuItems()"
        ".filter(i=>i.app?.get_id()===" + json.dumps(appid + ".desktop") + ").map(i=>i.message??'')"
    )


def status_row(appid, *, recording=False):
    # The portal lists every sandbox instance of the app: the UI process and,
    # when it runs, the browser broker (a separate `flatpak run` instance).
    # GNOME Shell merges them into one entry. Exactly one instance carries the
    # status, and the person sees exactly one Cassini entry showing it.
    def wanted(message):
        return message.startswith("Recording,") if recording else message == "Ready to record browser meetings"

    rows = portal_rows(appid)
    carrying = [row for row in rows if wanted(row.get("message", ""))]
    if not carrying:
        return None
    assert len(carrying) == 1, rows
    assert not [row for row in rows if row.get("message") and row not in carrying], rows
    entries = shell_entries(appid)
    if len(entries) != 1 or not wanted(entries[0]):
        return None
    return carrying[0]


def menu_item(appid, operation):
    # Exercise GNOME's real BackgroundAppMenuItem, including the close button
    # that dispatches org.gtk.Actions.Activate('quit'), not our app action helper.
    return shell(
        "(() => {const t=Main.panel.statusArea.quickSettings._backgroundApps.quickSettingsItems[0];"
        "const item=t._appsSection._getMenuItems().find(i=>i.app?.get_id()===" + json.dumps(appid + ".desktop") + ");"
        "if(!item)return false;Main.panel.statusArea.quickSettings.menu.open();t.menu.open();"
        + operation
        + ";return true;})()"
    )


def launch(appid):
    app = Gio.DesktopAppInfo.new(appid + ".desktop")
    assert app and app.launch([], None), appid
    until(lambda: owner(appid) and windows(appid), "app-grid activation " + appid)


def broker_configure(appid):
    # Arm through the production switch: its accepted change acquires the app
    # hold immediately, before the asynchronous broker write completes.
    action(appid, "preferences")

    def automatic():
        candidates = []
        for node in walk(Atspi.get_desktop(0)):
            if (
                (node.get_name() or "") == "Record every browser meeting automatically"
                and node.get_role_name() == "switch"
                and node.get_state_set().contains(Atspi.StateType.SENSITIVE)
                and node.get_state_set().contains(Atspi.StateType.SHOWING)
            ):
                rectangle = node.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
                if rectangle.width > 0:
                    candidates.append((rectangle.width, node))
        # AdwSwitchRow and its GtkSwitch share a name and role. The row's
        # ancestor action does not operate the switch reliably. D42's real
        # package-upgrade journey exercises the inner switch's own action.
        return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None

    switch = until(automatic, "automatic-recording switch ready")
    if not switch.get_state_set().contains(Atspi.StateType.CHECKED):
        assert switch.get_action_iface().do_action(0), "GtkSwitch action refused"
    until(
        lambda: automatic() and automatic().get_state_set().contains(Atspi.StateType.CHECKED),
        "automatic-recording change accepted",
    )
    # Close the real sheet's accessible button, not its parent MetaWindow.
    click("Close", dialog="Preferences")

    def dismissed():
        return not any(
            (n.get_name() or "") == "Record every browser meeting automatically"
            and n.get_state_set().contains(Atspi.StateType.SHOWING)
            for n in walk(Atspi.get_desktop(0))
        )

    try:
        until(dismissed, "Preferences dismissed")
    except AssertionError:
        for node in walk(Atspi.get_desktop(0)):
            if node.get_name() in ("Close", "Preferences", "Record every browser meeting automatically"):
                ancestors = []
                parent = node
                while parent:
                    ancestors.append((parent.get_name(), parent.get_role_name()))
                    parent = parent.get_parent()
                print("PREFS ORACLE", node.get_state_set().get_states(), ancestors, flush=True)
        call(
            "org.gnome.Shell.Screenshot",
            "/org/gnome/Shell/Screenshot",
            "org.gnome.Shell.Screenshot",
            "Screenshot",
            GLib.Variant("(bbs)", (False, False, "/out/preferences-debug.png")),
        )
        raise


def host_status_probe(appid):
    # Exercise the *installed* BackgroundRecording implementation, not source
    # imports or an identity-only stand-in. A separate peer needs its own grant.
    program = r"""
import json,logging,sys
from gi.repository import GLib
sys.path.insert(0,'/usr/share/cassini-viewer')
from cassini_viewer.background_recording import BackgroundRecording
messages=[]
class Capture(logging.Handler):
 def emit(self,record): messages.append(record.getMessage())
logger=logging.getLogger('cassini_viewer.background_recording')
logger.setLevel(logging.INFO);logger.addHandler(Capture())
portal=BackgroundRecording(sys.argv[1],native=True)
loop=GLib.MainLoop();result={}
def published(ok):
 result['statusAccepted']=ok;loop.quit()
def permitted(ok):
 result['permissionGranted']=ok
 if ok: portal.status('Ready to record browser meetings',published)
 else: loop.quit()
portal.request(permitted)
GLib.timeout_add_seconds(15,lambda:loop.quit() or False)
loop.run();portal.close();result['messages']=messages
print(json.dumps(result))
"""
    report = json.loads(subprocess.check_output(["python3", "-c", program, appid], text=True, timeout=20))
    assert report.get("permissionGranted") is True, report
    assert "statusAccepted" in report, report
    if not report["statusAccepted"]:
        assert any(
            "Only sandboxed applications can set background status" in message for message in report["messages"]
        ), report
    return report


def sandbox_diagnostic(appid):
    """Explain a missing Flatpak Background Apps row without changing the verdict.

    Reports the sandbox instances and their LSM labels, then asks the portal,
    from a second process in the same app sandbox, whether it accepts SetStatus.
    """
    report = {}
    for name, command in (
        ("instances", ["flatpak", "ps", "--columns=instance,pid,child-pid,application"]),
        ("runtimeInfo", ["sh", "-c", 'ls -la "$XDG_RUNTIME_DIR/.flatpak" "$XDG_RUNTIME_DIR"/.flatpak/*/ 2>&1']),
    ):
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        report[name] = (result.stdout + result.stderr).strip()
    labels = {}
    for line in report["instances"].splitlines():
        for value in line.split()[1:3]:
            if value.isdigit():
                try:
                    labels[value] = Path(f"/proc/{value}/attr/current").read_text().strip("\0\n ")
                except OSError as error:
                    labels[value] = str(error)
    report["labels"] = labels
    program = (
        "import json,sys;from gi.repository import GLib;sys.path.insert(0,'/app/share/cassini-viewer');"
        "from cassini_viewer.background_recording import BackgroundRecording;"
        "p=BackgroundRecording(sys.argv[1]);l=GLib.MainLoop();r={};"
        "s=lambda ok:(r.__setitem__('status',ok),l.quit());"
        "g=lambda ok:(r.__setitem__('granted',ok),p.status('QA probe',s) if ok else l.quit());"
        "p.request(g);GLib.timeout_add_seconds(15,l.quit);l.run();print(json.dumps(r))"
    )
    result = subprocess.run(
        ["flatpak", "run", "--user", "--command=python3", appid, "-c", program, appid],
        capture_output=True,
        text=True,
        timeout=40,
    )
    report["sandboxProbe"] = result.stdout.strip() or result.stderr.strip()[-2000:]
    return report


def background_row(appid, check, label):
    """Wait for a Background Apps row; on timeout, report what the portal had."""
    try:
        return until(check, label)
    except AssertionError:
        rows = call(
            "org.freedesktop.background.Monitor",
            "/org/freedesktop/background/monitor",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.freedesktop.background.Monitor", "BackgroundApps")),
        )[0]
        print(
            "BACKGROUND STATUS",
            dict(label=label, app=appid, owner=owner(appid), windows=windows(appid), rows=rows),
            sandbox_diagnostic(appid),
            flush=True,
        )
        screenshot("background-status-failed.png")
        raise


def idle_notifications(appid):
    return shell(
        "Main.messageTray.getSources().filter(s=>s._appId==="
        + json.dumps(appid)
        + ").flatMap(s=>s.notifications??[]).filter(n=>n.title==='Cassini is still running').length"
    )


def screenshot(name):
    call(
        "org.gnome.Shell.Screenshot",
        "/org/gnome/Shell/Screenshot",
        "org.gnome.Shell.Screenshot",
        "Screenshot",
        GLib.Variant("(bbs)", (False, False, str(OUT / name))),
    )


def capture_inputs():
    sources = {
        s["index"]: s["name"]
        for s in json.loads(subprocess.check_output(["pactl", "--format=json", "list", "sources"], text=True))
    }
    outputs = json.loads(subprocess.check_output(["pactl", "--format=json", "list", "source-outputs"], text=True))
    return sorted(sources.get(output["source"], "unknown") for output in outputs)


def measured_privacy(inputs):
    until(lambda: capture_inputs() == inputs, "actual capture inputs")
    until(
        lambda: (
            shell("Main.panel.statusArea.quickSettings._volumeInput._control.get_source_outputs().length")
            == len(inputs)
        ),
        "GNOME observes capture streams",
    )
    return privacy_indicator()


def privacy_indicator():
    return shell(
        "(() => {const i=Main.panel.statusArea.quickSettings._volumeInput._indicator;"
        "return {visible:i.visible,mapped:i.mapped,privacy:i.has_style_class_name('privacy-indicator')};})()"
    )


def selected(manifest, me, others):
    events = json.loads(manifest.read_text()).get("events", [])
    selection = next((event for event in reversed(events) if event["type"] == "selection"), {})
    return selection.get("me") is me and selection.get("others") is others


def pointer_move(x, y, label):
    """Place the compositor's virtual pointer at stage coordinates."""

    def pointer_ready():
        pointer = shell("global.get_pointer()")
        if abs(pointer[0] - x) < 2 and abs(pointer[1] - y) < 2:
            return True
        # Mutter initializes a headless virtual pointer on its first motion;
        # it may ignore that event's x coordinate. Moving is idempotent: keep
        # placing it until the compositor reports the actual target position.
        shell(
            "(() => {global._cassiniQaPointer??=imports.gi.Clutter.get_default_backend().get_default_seat().create_virtual_device("
            "imports.gi.Clutter.InputDeviceType.POINTER_DEVICE);"
            f"global._cassiniQaPointer.notify_absolute_motion(imports.gi.GLib.get_monotonic_time(),{x},{y});return true;}})()"
        )
        return False

    until(pointer_ready, "pointer reaches " + label)


def pointer_click(x, y, label):
    """Primary click at stage coordinates through a compositor virtual pointer."""
    pointer_move(x, y, label)
    # Clutter button 1 is the primary button; Mutter maps it to BTN_LEFT.
    assert shell(
        "(() => {const t=imports.gi.GLib.get_monotonic_time();"
        "global._cassiniQaPointer.notify_button(t,1,imports.gi.Clutter.ButtonState.PRESSED);"
        "imports.gi.GLib.idle_add(imports.gi.GLib.PRIORITY_DEFAULT_IDLE,()=>{"
        "global._cassiniQaPointer.notify_button(imports.gi.GLib.get_monotonic_time(),1,imports.gi.Clutter.ButtonState.RELEASED);"
        "return imports.gi.GLib.SOURCE_REMOVE;});return true;})()"
    )


def choose_recording_sources(manifest, label, me, others, appid):
    click("Change what is recorded")
    assert shell(
        "(() => {const w=imports.gi.Shell.AppSystem.get_default().lookup_app("
        + json.dumps(appid + ".desktop")
        + ")?.get_windows()[0];if(!w)return false;w.activate(global.get_current_time());return true;})()"
    )

    def radio():
        candidates = []
        for node in walk(Atspi.get_desktop(0)):
            if node.get_name() != label or node.get_role_name() != "radio button":
                continue
            state = node.get_state_set()
            if not state.contains(Atspi.StateType.SHOWING) or not state.contains(Atspi.StateType.SENSITIVE):
                continue
            rectangle = node.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
            if rectangle.width > 0:
                candidates.append((rectangle.width, node))
        return min(candidates, key=lambda value: value[0])[1] if candidates else None

    choice = until(radio, "source radio " + label)
    # Wayland GTK cannot supply global coordinates or implement AT-SPI
    # GrabFocus for these radio prefixes. Click the real row using its window
    # coordinates and the private compositor's authoritative window position.
    row = choice
    current = choice.get_parent()
    while current and current.get_role_name() not in ("radio group", "dialog"):
        box = current.get_component_iface().get_extents(Atspi.CoordType.WINDOW)
        if box.width > 100 and 20 < box.height < 100:
            row = current
        current = current.get_parent()
    box = row.get_component_iface().get_extents(Atspi.CoordType.WINDOW)
    frame = shell(
        "(() => {const w=imports.gi.Shell.AppSystem.get_default().lookup_app("
        + json.dumps(appid + ".desktop")
        + ").get_windows()[0];const r=w.get_frame_rect();return {x:r.x,y:r.y};})()"
    )
    x, y = frame["x"] + box.x + box.width / 2, frame["y"] + box.y + box.height / 2

    try:
        # The dialog may still be animating in when the first click arrives,
        # and GTK then drops it. Selecting a radio row is idempotent, so click
        # again (at most three times) until the session records the choice.
        for attempt in range(3):
            pointer_click(x, y, "source row " + label)
            try:
                until(lambda: selected(manifest, me, others), "source selection " + label, timeout=4)
                break
            except AssertionError:
                if attempt == 2:
                    raise
    except AssertionError:
        print(
            "SOURCE CLICK",
            dict(
                target=[x, y],
                frame=frame,
                box=[box.x, box.y, box.width, box.height],
                pointer=shell("global.get_pointer()"),
                states=[str(state) for state in choice.get_state_set().get_states()],
            ),
            flush=True,
        )
        call(
            "org.gnome.Shell.Screenshot",
            "/org/gnome/Shell/Screenshot",
            "org.gnome.Shell.Screenshot",
            "Screenshot",
            GLib.Variant("(bbs)", (True, False, str(OUT / "source-selection-failed.png"))),
        )
        raise
    click("Done", dialog="What is recorded")


def recording(root):
    for manifest in sorted(root.glob("Recording-*/session.json")):
        value = json.loads(manifest.read_text())
        # The generated microphone must deliver PCM itself; frames on the
        # system track alone once hid a stalled (non-live) test source.
        microphone = any(
            track.get("kind") == "microphone" and track.get("frames", 0) > 0 for track in value.get("tracks", [])
        )
        if microphone and value.get("state") == "recording":
            return manifest
    return None


def prepare_desktop():
    until(lambda: (Path(os.environ["XDG_RUNTIME_DIR"]) / "wayland-0").exists(), "private Wayland socket")
    os.environ["WAYLAND_DISPLAY"] = "wayland-0"
    # Let Shell finish its startup animation first. Hiding the overview while it
    # runs can leave layoutManager._startingUp set, and the AppIndicator host
    # then waits for 'startup-complete' forever before adding any icon.
    until(lambda: shell("!Main.layoutManager._startingUp"), "GNOME Shell startup complete")
    until(lambda: shell("Main.overview.hide(); true"), "GNOME Shell ready")
    until(
        lambda: shell("!!Main.panel.statusArea.quickSettings._volumeInput?._indicator"), "GNOME audio indicators ready"
    )
    version = subprocess.check_output(["gnome-shell", "--version"], text=True).strip()
    assert version.startswith("GNOME Shell 50"), version
    # Create the virtual pointer before any Shell menu takes a modal grab: a
    # device first used under a menu's grab had its first click ignored.
    pointer_move(640, 400, "parked pointer")
    # PipeWire's shipped pipewire.conf documents the adapter/audiotestsrc
    # factory. It generates microphone PCM without opening a loopback capture
    # stream, so an idle privacy indicator remains a meaningful baseline.
    # live = true makes it its own timer-driven driver; a non-live source has
    # no driver in a mic-to-Cassini graph and never produces a frame.
    config = HOME / ".config/pipewire/pipewire.conf.d"
    config.mkdir(parents=True, exist_ok=True)
    (config / "90-cassini-qa-source.conf").write_text(
        "context.spa-libs = { audiotestsrc = audiotestsrc/libspa-audiotestsrc }\n"
        "context.objects = [{ factory = adapter args = { factory.name = audiotestsrc "
        'node.name = mic node.description = "QA microphone" media.class = Audio/Source '
        "audio.position = [ MONO ] audio.rate = 48000 node.always-process = true "
        "node.param.Props = { live = true } } }]\n"
    )
    subprocess.Popen(["pipewire"], stdout=(OUT / "pipewire.log").open("w"), stderr=subprocess.STDOUT)
    until(lambda: (Path(os.environ["XDG_RUNTIME_DIR"]) / "pipewire-0").exists(), "PipeWire")
    subprocess.Popen(["wireplumber"], stdout=(OUT / "wireplumber.log").open("w"), stderr=subprocess.STDOUT)
    subprocess.Popen(["pipewire-pulse"], stdout=(OUT / "pulse.log").open("w"), stderr=subprocess.STDOUT)
    until(lambda: subprocess.run(["pactl", "info"], capture_output=True).returncode == 0, "private Pulse server")
    for args, resource, name in (
        (["load-module", "module-null-sink", "sink_name=voice"], "sinks", "voice"),
        (["load-module", "module-null-sink", "sink_name=speakers"], "sinks", "speakers"),
    ):
        subprocess.run(["pactl", *args], check=True, capture_output=True)
        until(
            lambda resource=resource, name=name: any(
                line.split()[1] == name
                for line in subprocess.check_output(["pactl", "list", "short", resource], text=True).splitlines()
            ),
            "private audio " + name,
        )
    until(
        lambda: any(
            line.split()[1] == "mic"
            for line in subprocess.check_output(["pactl", "list", "short", "sources"], text=True).splitlines()
        ),
        "private virtual microphone",
    )
    for args in (["set-default-source", "mic"], ["set-default-sink", "speakers"]):
        diagnostic = []

        def set_default(args=args, diagnostic=diagnostic):
            result = subprocess.run(["pactl", *args], capture_output=True, text=True)
            diagnostic[:] = [result.stderr.strip()]
            return result.returncode == 0

        try:
            until(set_default, "private audio default " + args[1])
        except AssertionError as error:
            raise AssertionError(
                f"{error}: {diagnostic}; sources="
                + subprocess.check_output(["pactl", "list", "short", "sources"], text=True)
            ) from error
    tone = OUT / "tone.wav"
    with wave.open(str(tone), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        import struct

        wav.writeframes(
            b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000))
        )
    subprocess.Popen(
        ["sh", "-c", 'while true; do paplay --device=speakers "$1"; done', "cassini-qa-tone", str(tone)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return version


def main():
    version = prepare_desktop()
    versions = subprocess.check_output(
        ["dpkg-query", "-W", "xdg-desktop-portal", "xdg-desktop-portal-gnome", "flatpak"], text=True
    )
    results = {"shell": version, "packages": versions, "cases": []}
    root = HOME / "Music/Cassini"
    for appid in ("com.gocassini.Viewer", "com.gocassini.Viewer.Devel"):
        flatpak = appid.endswith(".Devel")
        prefix = ["flatpak", "run", "--user", "--command=gsettings", appid] if flatpak else []
        subprocess.run(
            [*prefix, *([] if flatpak else ["gsettings"]), "set", "com.gocassini.Viewer", "skip-record-first", "true"],
            check=True,
        )
        # Explicit pre-consent isolates the Background Apps journey from the desktop
        # permission prompt. The real RequestBackground still runs in the app; its
        # denial/late-response behavior is covered by the private-bus GTK test.
        if flatpak:
            subprocess.run(["flatpak", "permission-set", "background", "background", appid, "yes"], check=True)
        launch(appid)
        original_pid = pid(appid)
        broker_configure(appid)
        close_window(appid)
        until(lambda appid=appid: not windows(appid), "last window closed")
        assert owner(appid), "Automatic recording did not keep the process alive"
        case = {"app": appid, "originalPID": original_pid}
        probe = None if flatpak else host_status_probe(appid)
        if probe:
            case["hostPortal"] = probe
        can_list = flatpak  # D41.3: native builds use SNI / app-grid, never claim Background Apps.
        if can_list:
            row = background_row(
                appid, lambda appid=appid: status_row(appid), "watching status in real Background Apps"
            )
            assert row.get("message") == "Ready to record browser meetings", row
            case["idleStatus"] = row["message"]
            case["portalInstances"] = len(portal_rows(appid))
            case["shellEntries"] = shell_entries(appid)
            until(lambda appid=appid: idle_notifications(appid), "one-time still-running notification")
            assert idle_notifications(appid) == 1
            until(lambda appid=appid: menu_item(appid, "true"), "Background Apps menu")
            screenshot("background-apps-idle.png")
            until(lambda appid=appid: menu_item(appid, "item.activate(null)"), "Background Apps Open")
            until(lambda appid=appid: windows(appid), "Library reopened from Background Apps")
            assert pid(appid) == original_pid
            case["backgroundOpen"] = True
        else:
            # Match the documented upstream rejection rather than fabricating an
            # entry. The installed package must still reopen via its real launcher.
            assert listed(appid) is None
            case["backgroundOpen"] = False
            case["limitation"] = "Host applications are excluded by xdg-desktop-portal Background (R48)"
            launch(appid)
            assert pid(appid) == original_pid
        case["appGridReusesProcess"] = True
        if can_list:
            # Both native activation routes must work, even after the last window
            # has gone and only automatic recording keeps the process alive.
            close_window(appid)
            until(lambda appid=appid: not windows(appid), "second close")
            background_row(appid, lambda appid=appid: listed(appid), "second Background Apps listing")
            launch(appid)
            assert pid(appid) == original_pid
            assert idle_notifications(appid) == 1
            case["oneTimeNotification"] = True
        action(appid, "record")
        manifest = until(lambda: recording(root), "actual microphone frames")
        case["meeting"] = manifest.parent.name
        close_window(appid)
        click("Keep Recording in Background")
        until(lambda appid=appid: not windows(appid), "recording hidden")
        if can_list:
            row = background_row(
                appid, lambda appid=appid: status_row(appid, recording=True), "live recording Background Apps status"
            )
            case["recordingStatus"] = row["message"]
            until(lambda appid=appid: menu_item(appid, "true"), "recording Background Apps menu")
            screenshot("background-apps-recording.png")
            until(
                lambda appid=appid: menu_item(appid, "item.get_last_child().emit('clicked', 1)"),
                "Background Apps close button",
            )
        else:
            action(appid, "quit")
        try:
            until(lambda appid=appid: not owner(appid), "stop and keep, then process exit")
        except AssertionError:
            print(
                "QUIT DIAGNOSTIC",
                dict(
                    app=appid,
                    windows=windows(appid),
                    inputs=capture_inputs(),
                    manifest=json.loads(manifest.read_text()),
                ),
                flush=True,
            )
            if not flatpak:
                # The installed app registers faulthandler on SIGUSR1 (main_loop_watchdog).
                os.kill(pid(appid), signal.SIGUSR1)
                time.sleep(2)
            raise
        saved = json.loads(manifest.read_text())
        assert saved["state"] == "completed"
        assert any(track["frames"] > 0 for track in saved["tracks"])
        for track in saved["tracks"]:
            with wave.open(str(manifest.parent / track["file"]), "rb") as wav:
                assert wav.getnframes() == track["frames"]
        case["quitKeptAudio"] = True
        case["quitHadNoWindow"] = True
        results["cases"].append(case)
        (OUT / "background-apps.json").write_text(json.dumps(results, indent=2) + "\n")
    assert len(results["cases"]) == 2
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
