#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""WebKit rendering spike in a private headless GNOME/Wayland QA desktop.

Reuses PR #310's synthetic Opus generator and anchor/frame-clock method, but
injects FINGER scroll events through Mutter, exercising WebKit's real scroller.
Never run on a personal desktop. CASSINI_WEBKIT_QA=1 is required.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if os.environ.get("CASSINI_WEBKIT_QA") != "1":
    raise SystemExit("Run only inside the private WebKit QA desktop")

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gio, GLib, Gst, Gtk, WebKit  # noqa: E402
from transcript_scroll_support import hour_meeting  # noqa: E402

build = Path(os.environ["CASSINI_TEST_BUILD"])
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402

OUT = Path(os.environ["CASSINI_WEBKIT_OUT"])
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "environment.json").write_text(
    json.dumps(
        dict(
            gtk=[Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()],
            adwaita=[Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()],
            webkit=[WebKit.get_major_version(), WebKit.get_minor_version(), WebKit.get_micro_version()],
            gstreamer=Gst.version_string(),
            python=sys.version,
            renderer=os.environ.get("GSK_RENDERER"),
            desktop=subprocess.check_output(["gnome-shell", "--version"], text=True).strip(),
        ),
        indent=2,
    )
)
library_root = OUT / "empty-library"
library_root.mkdir(exist_ok=True)
schemas = OUT / "schemas"
schemas.mkdir(exist_ok=True)
shutil.copyfile(ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml")
subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
os.environ.update(
    GSETTINGS_SCHEMA_DIR=str(schemas),
    GSETTINGS_BACKEND="memory",
    CASSINI_LIBRARY_DIR=str(library_root),
    CASSINI_TRANSCRIPT_RENDER="webkit",
)
context = GLib.MainContext.default()


def until(predicate):
    # A deadline is a failure bound, never an observation/readiness condition.
    expired = []
    deadline = GLib.timeout_add_seconds(60, lambda: (expired.append(True), GLib.SOURCE_REMOVE)[1])
    try:
        while not predicate():
            if expired:
                raise TimeoutError("QA readiness deadline")
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(deadline)


def paint(window, count=1):
    for _ in range(count):
        painted = []
        clock = window.get_frame_clock()
        handler = clock.connect("after-paint", lambda *_, painted=painted: painted.append(True))
        window.add_tick_callback(lambda *_: GLib.SOURCE_REMOVE)
        try:
            until(lambda painted=painted: painted)
        finally:
            clock.disconnect(handler)


def js(renderer, script):
    result = []
    renderer.evaluate(script, lambda value: result.append(value))
    until(lambda: result)
    assert result[0] is not None, script
    return result[0]


bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def shell(script):
    result = bus.call_sync(
        "org.gnome.Shell",
        "/org/gnome/Shell",
        "org.gnome.Shell",
        "Eval",
        GLib.Variant("(s)", (script,)),
        GLib.VariantType.new("(bs)"),
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    )
    ok, answer = result.unpack()
    assert ok, answer
    return answer


def move_pointer(window, x, y):
    shell(f"global.webkitPointer.notify_absolute_motion(imports.gi.GLib.get_monotonic_time(),{x},{y});true")
    # A cold headless seat can clamp the first absolute motion at an edge.
    # Observe the compositor's actual pointer, then correct through native
    # relative motion; never assume the requested coordinate was delivered.
    for _ in range(6):
        paint(window, 2)
        pointer = json.loads(json.loads(shell("JSON.stringify(global.get_pointer())")))
        dx, dy = x - pointer[0], y - pointer[1]
        if abs(dx) <= 1 and abs(dy) <= 1:
            return
        shell(f"global.webkitPointer.notify_relative_motion(imports.gi.GLib.get_monotonic_time(),{dx},{dy});true")
    raise AssertionError(f"Native pointer did not reach the transcript: {pointer}")


def resident_processes():
    # Sum RSS explicitly (shared pages are counted per process). WebProcess
    # and NetworkProcess live in descendants behind WebKit's sandbox bwrap.
    nodes = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            fields = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
            nodes[int(proc.name)] = (
                int(fields["PPid"]),
                int(fields.get("VmRSS", "0 kB").split()[0]),
                fields["Name"].strip(),
            )
        except (OSError, ValueError):
            pass
    descendants = {os.getpid()}
    while True:
        extra = {pid for pid, (parent, _rss, _name) in nodes.items() if parent in descendants} - descendants
        if not extra:
            break
        descendants.update(extra)
    return [dict(pid=pid, rssKiB=nodes[pid][1], name=nodes[pid][2]) for pid in sorted(descendants) if pid in nodes]


def summarize(frames, phase):
    selected = [f for f in frames if f["phase"] == phase]
    pairs = [(a, b) for a, b in zip(selected, selected[1:], strict=False) if a["direction"] == b["direction"]]
    reversals = sum(b["direction"] * (b["y"] - a["y"]) < -0.5 for a, b in pairs)
    anchor_reversals = sum(
        b["direction"] * (b["anchor"][0] - a["anchor"][0]) < 0
        or (b["anchor"][0] == a["anchor"][0] and b["direction"] * (b["anchor"][1] - a["anchor"][1]) < -0.5)
        for a, b in pairs
    )
    return dict(
        frames=len(selected),
        reversals=reversals,
        anchorReversals=anchor_reversals,
        upperValues=sorted(set(f["upper"] for f in selected)),
        worstFrameIntervalMs=max((f["dt"] for f in selected), default=0),
    )


app = CassiniApplication("com.gocassini.WebkitRenderingSpike", "test", "development")
app.transcription_enabled = False
app.register(None)
app.activate()
window = app.get_active_window()
until(lambda: not window._pending)
window.monitor.callback = lambda *_: None
window.maximize()
window.present()
paint(window, 3)
shell(
    "global.webkitPointer = imports.gi.Clutter.get_default_backend().get_default_seat().create_virtual_device(imports.gi.Clutter.InputDeviceType.POINTER_DEVICE); true"
)
reports = []
cases = [("hour", 1, False), ("four-hour", 4, False), ("hour-plain", 1, True)]
requested = set(sys.argv[1:])
input_mode = os.environ.get("CASSINI_WEBKIT_INPUT", "mutter")
for name, hours, plain in cases:
    if requested and name not in requested:
        continue
    available = int(
        next(
            line.split()[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
    )
    assert available > 4000 * 1024, f"RAM rule: only {available / 1024:.0f} MB available"
    fixture_dir = OUT / name
    opus = fixture_dir / "synthetic-hour.opus"
    if not opus.exists():
        opus = hour_meeting(fixture_dir, mixed=True, hours=hours, plain=plain)
    meeting = load_open_meeting(opus, library_root)
    os.environ["CASSINI_TRANSCRIPT_WEBKIT_PLAIN"] = "1" if plain else "0"
    assert 11000 * hours < len(meeting.words) < 14000 * hours
    if not plain:
        assert len(meeting.speakers) == 2 and any(row.asides for row in meeting.turns)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    continuous_tick = window.add_tick_callback(lambda *_: GLib.SOURCE_CONTINUE)
    paint(window)
    frame_cpu, frame_intervals = [], []
    started = [0]
    clock = window.get_frame_clock()
    last_frame = [clock.get_frame_time()]

    def before_paint(frame_clock, frame_intervals=frame_intervals, last_frame=last_frame, started=started):
        now = frame_clock.get_frame_time()
        frame_intervals.append((now - last_frame[0]) / 1000)
        last_frame[0] = now
        started[0] = time.thread_time_ns()

    before = clock.connect("before-paint", before_paint)
    after = clock.connect(
        "after-paint",
        lambda *_, frame_cpu=frame_cpu, started=started: frame_cpu.append((time.thread_time_ns() - started[0]) / 1e6),
    )
    load_cpu = time.thread_time_ns()
    opened = GLib.get_monotonic_time()
    window.open_meeting(meeting, external=True)
    page = window.meeting_page
    renderer = page.web_transcript
    until(lambda renderer=renderer: renderer.ready)
    controllers = renderer.view.observe_controllers()
    native_scroll = next(
        controllers.get_item(i)
        for i in range(controllers.get_n_items())
        if isinstance(controllers.get_item(i), Gtk.EventControllerScroll)
    )
    (OUT / f"{name}-controllers.json").write_text(
        json.dumps(
            [
                dict(
                    type=type(controllers.get_item(i)).__name__,
                    phase=int(controllers.get_item(i).get_propagation_phase()),
                )
                for i in range(controllers.get_n_items())
            ],
            indent=2,
        )
    )
    paint(window, 2)
    load_elapsed = (clock.get_frame_time() - opened) / 1000
    load_cpu_ms = (time.thread_time_ns() - load_cpu) / 1e6
    load_worst_cpu = max(frame_cpu, default=0)
    load_worst_interval = max(frame_intervals, default=0)
    # A snapshot is paint evidence, not a guessed delay or an empty-widget
    # after-paint signal. Its completion gives a conservative first-paint bound.
    snapshots = []
    renderer.view.get_snapshot(
        WebKit.SnapshotRegion.VISIBLE,
        WebKit.SnapshotOptions.NONE,
        None,
        lambda view, result, snapshots=snapshots: snapshots.append(view.get_snapshot_finish(result)),
    )
    until(lambda snapshots=snapshots: snapshots)
    paint(window)
    first_paint_upper = (clock.get_frame_time() - opened) / 1000
    assert snapshots[0].save_to_png(str(OUT / f"{name}-first-paint.png"))
    js(renderer, (ROOT / "tests/ui/webkit_measurements.js").read_text())
    js(renderer, "scrollTo(0,2500)")
    paint(window, 3)
    initial_upper = js(renderer, "document.documentElement.scrollHeight").to_double()
    if name == "hour":
        with (OUT / "accessibility.json").open("w") as evidence:
            inspector = subprocess.Popen(
                [sys.executable, str(ROOT / "tests/ui/webkit_accessibility.py")], stdout=evidence
            )
            until(lambda inspector=inspector: inspector.poll() is not None)
            assert inspector.returncode == 0
    page._follow_until = 10**30
    page.settings.set_boolean("follow-along", False)
    until(lambda: window.player._prerolled)
    # Per-word seek, folded-chip seek, native selection/copy, search, follow,
    # CSP and navigation are checked on the real DOM before scroll sampling.
    first = meeting.turns[0].words[0].start_ms
    js(renderer, "document.querySelector('.cassini-word').click()")
    until(lambda first=first: abs(window.player.position - first) < 100)
    chip = next((aside for row in meeting.turns for _, aside in row.asides), None)
    if chip:
        js(renderer, "document.querySelector('.mv-interjection .cassini-word').click()")
        until(lambda chip=chip: abs(window.player.position - chip.words[0].start_ms) < 100)
    js(renderer, "cassini.highlight(0)")
    assert js(renderer, "document.querySelectorAll('.current').length").to_int32() >= 1
    assert js(renderer, "cassini.search('internationalization')").to_int32() > 0
    js(renderer, f"cassini.search('');cassini.follow({min(200, len(meeting.turns) - 1)})")
    assert js(renderer, "scrollY").to_double() > 0
    # Probe denied subresource requests in a page with the actual production CSP.
    assert js(renderer, "typeof fetch === 'function'").to_boolean()
    js(
        renderer,
        "window.cspViolations=[];document.addEventListener('securitypolicyviolation',e=>cspViolations.push(e.violatedDirective));fetch('https://example.invalid/cassini-offline-probe').catch(()=>{});let img=new Image();img.src='https://example.invalid/cassini-probe.png';document.body.append(img)",
    )
    paint(window, 3)
    csp = json.loads(js(renderer, "JSON.stringify(cspViolations)").to_string())
    assert "connect-src" in csp and "img-src" in csp, csp
    js(renderer, "document.querySelector('img').remove();scrollTo(0,2500)")
    # A fully selected word remains text, even though it is an interactive button.
    selected = js(
        renderer,
        "(()=>{let r=document.createRange();r.selectNodeContents(document.querySelector('.cassini-word'));getSelection().removeAllRanges();getSelection().addRange(r);return getSelection().toString()})()",
    ).to_string()
    assert selected == meeting.turns[0].items[0]
    copied = []
    clipboard = window.get_display().get_clipboard()
    clipboard_changed = []
    changed_handler = clipboard.connect("changed", lambda *_, changed=clipboard_changed: changed.append(True))
    renderer.view.execute_editing_command("Copy")
    until(lambda changed=clipboard_changed: changed)
    clipboard.disconnect(changed_handler)
    clipboard.read_text_async(
        None, lambda clipboard, result, copied=copied: copied.append(clipboard.read_text_finish(result))
    )
    until(lambda copied=copied: copied)
    assert copied[0] == selected
    js(renderer, "getSelection().removeAllRanges()")
    # Exercise the actual player -> MeetingPage -> word-follow bridge, not
    # only the direct row-jump helper. Use a long paragraph away from the top.
    follow_row = next(
        row for row in meeting.turns if len(row.words) >= 60 and row.start_ms > (meeting.duration_ms or 0) // 3
    )
    page.settings.set_boolean("follow-along", True)
    page._follow_until = 0
    js(renderer, "scrollTo(0,0)")
    window.player.seek(follow_row.start_ms)
    window.player.play()
    until(lambda: window.player.playing)
    for _ in range(60):
        paint(window)
        if js(
            renderer,
            "Array.from(document.querySelectorAll('.current')).some(w=>{let r=w.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})",
        ).to_boolean():
            break
    else:
        raise AssertionError("Playback follow did not reveal a sounding word")
    window.player.pause()
    page.settings.set_boolean("follow-along", False)
    page._follow_until = 10**30
    gestures = []
    for playback in (False, True):
        if playback:
            window.player.seek(0)
            window.player.play()
            until(lambda: window.player.playing)
        else:
            window.player.pause()
        phase = "playing" if playback else "paused"
        js(renderer, f"probe.phase={json.dumps(phase)};probe.direction=0;scrollTo(0,2500)")
        paint(window, 3)
        if input_mode != "dom-smooth":
            move_pointer(window, 900, 350)
        for flick in range(6):
            shell(
                "global.webkitPointer.notify_scroll_continuous(imports.gi.GLib.get_monotonic_time(),0,0,imports.gi.Clutter.ScrollSource.FINGER,imports.gi.Clutter.ScrollFinishFlags.HORIZONTAL|imports.gi.Clutter.ScrollFinishFlags.VERTICAL);true"
            )
            direction = 1 if flick % 2 == 0 else -1
            # Reset between independent gestures outside the scored phase, so
            # no test reaches a boundary and mistakes elastic rebound for a
            # layout jump. Direction comes from the real wheel DOM event.
            js(renderer, "probe.phase='idle';probe.direction=0;scrollTo(0,document.documentElement.scrollHeight/2)")
            paint(window, 5)
            js(renderer, f"probe.phase={json.dumps(phase)}")
            initial_y = js(renderer, "scrollY").to_double()
            if input_mode == "dom-smooth":
                js(renderer, f"probe.direction={direction}")
            if input_mode == "controller":
                native_scroll.emit("scroll-begin")
            deltas = (75, 138, 225, 213, 163) if input_mode == "dom-smooth" else (2, 4, 6, 8, 10)
            for delta in deltas:
                if input_mode == "dom-smooth":
                    js(renderer, f"scrollBy({{top:{direction * delta},behavior:'instant'}})")
                elif input_mode == "controller":
                    native_scroll.emit("scroll", 0, direction * delta)
                else:
                    shell(
                        f"global.webkitPointer.notify_scroll_continuous(imports.gi.GLib.get_monotonic_time(),0,{direction * delta},imports.gi.Clutter.ScrollSource.FINGER,imports.gi.Clutter.ScrollFinishFlags.NONE);true"
                    )
                paint(window)
            released_y = js(renderer, "scrollY").to_double()
            shell(
                "global.webkitPointer.notify_scroll_continuous(imports.gi.GLib.get_monotonic_time(),0,0,imports.gi.Clutter.ScrollSource.FINGER,imports.gi.Clutter.ScrollFinishFlags.HORIZONTAL|imports.gi.Clutter.ScrollFinishFlags.VERTICAL);true"
            )
            if input_mode == "controller":
                native_scroll.emit("scroll-end")
                native_scroll.emit("decelerate", 0, direction * 100)
            elif input_mode == "dom-smooth":
                # Secondary geometry audit only: the browser owns this smooth
                # animation, but it is not a certification of touchpad kinetics.
                js(renderer, f"scrollBy({{top:{direction * 3000},behavior:'smooth'}})")
            # Observe the engine coast until twelve consecutive unchanged
            # browser frame samples, rather than guessing a coast duration.
            for _ in range(240):
                paint(window)
                if js(
                    renderer, "probe.frames.length>=12 && probe.frames.slice(-12).every(f=>f.y===probe.frames.at(-1).y)"
                ).to_boolean():
                    break
            else:
                raise AssertionError("Native kinetic coast did not settle")
            final_y = js(renderer, "scrollY").to_double()
            gestures.append(
                dict(
                    playback=playback,
                    flick=flick,
                    direction=direction,
                    initial=initial_y,
                    released=released_y,
                    final=final_y,
                    coast=direction * (final_y - released_y),
                    moved=direction * (released_y - initial_y) > 1,
                )
            )
        window.player.pause()
        js(renderer, "probe.phase='idle';probe.direction=0")
        print(json.dumps(dict(case=name, completed=phase, gestures=gestures[-6:])), flush=True)
    processes = resident_processes()
    rss = sum(p["rssKiB"] for p in processes)
    assert rss < 2.1 * 1024 * 1024, f"RAM budget: renderer descendants use {rss / 1024:.0f} MiB"
    js(renderer, "probe.phase='resize'")
    window.unmaximize()
    for width, height in ((960, 720), (1120, 760)):
        previous_width = window.get_width()
        window.set_default_size(width, height)
        # Default size excludes CSD/shadow extents on this desktop. Observe
        # the real allocation rather than equating it to the requested size.
        until(lambda previous_width=previous_width: window.get_width() != previous_width)
        stable_width, stable_frames = window.get_width(), 0
        for _ in range(120):
            paint(window)
            actual_width = window.get_width()
            stable_frames = stable_frames + 1 if actual_width == stable_width else 0
            stable_width = actual_width
            wanted_width = renderer.view.get_width()
            if (
                stable_frames >= 3
                and js(
                    renderer,
                    f"probe.frames.length>=3&&probe.frames.slice(-3).every(f=>f.width==={wanted_width}&&f.upper===probe.frames.at(-1).upper)",
                ).to_boolean()
            ):
                break
        else:
            raise AssertionError("Browser resize did not settle at the allocated width")
    js(renderer, "probe.phase='idle'")
    payload = json.loads(js(renderer, "JSON.stringify({frames:probe.frames,highlights:probe.highlights})").to_string())
    highlights = payload["highlights"]
    summary = dict(
        case=name,
        inputMode=input_mode,
        words=len(meeting.words),
        rows=len(meeting.turns),
        speakers=len(meeting.speakers),
        loadReadyFrameMs=load_elapsed,
        loadMainThreadCpuMs=load_cpu_ms,
        loadWorstGtkFrameCpuMs=load_worst_cpu,
        firstPaintObservedUpperBoundMs=first_paint_upper,
        fullyLaidOutMs=load_elapsed,
        loadWorstGtkFrameIntervalMs=load_worst_interval,
        initialUpper=initial_upper,
        paused=summarize(payload["frames"], "paused"),
        playing=summarize(payload["frames"], "playing"),
        resize=summarize(payload["frames"], "resize"),
        highlightJsMaxMs=max(highlights, default=0),
        highlightCalls=len(highlights),
        highlightJsMeanMs=sum(highlights) / max(1, len(highlights)),
        highlightIpcSubmitMaxCpuMs=max(renderer.tick_cpu_ms, default=0),
        highlightIpcSubmitMeanCpuMs=sum(renderer.tick_cpu_ms) / max(1, len(renderer.tick_cpu_ms)),
        mainThreadWorstGtkFrameCpuMs=max(frame_cpu, default=0),
        rssSumMiB=rss / 1024,
        processes=processes,
        gestures=gestures,
        postReleaseMovesDown=sum(g["direction"] > 0 and g["coast"] > 1 for g in gestures),
        postReleaseMovesUp=sum(g["direction"] < 0 and g["coast"] > 1 for g in gestures),
        invalidGestures=sum(not g["moved"] for g in gestures),
        cspViolations=csp,
    )
    summary["nativeKineticCertified"] = (
        input_mode == "mutter"
        and all(g["moved"] and g["coast"] > 1 for g in gestures)
        and summary["paused"]["anchorReversals"] == summary["playing"]["anchorReversals"] == 0
        and summary["paused"]["upperValues"] == summary["playing"]["upperValues"] == [initial_upper]
    )
    (OUT / f"{name}.json").write_text(json.dumps(dict(summary=summary, raw=payload), indent=2))
    reports.append(summary)
    print(json.dumps(summary), flush=True)
    clock.disconnect(before)
    clock.disconnect(after)
    window.remove_tick_callback(continuous_tick)
    window.back_to_library()
    window.maximize()
    paint(window, 3)
(OUT / "summary.json").write_text(json.dumps(reports, indent=2))
app.on_quit()
app.do_shutdown()
certified = bool(reports) and all(report["nativeKineticCertified"] for report in reports)
print(f"WebKit functional checks passed; native kinetic certified: {certified}", flush=True)
if input_mode != "dom-smooth" and not certified:
    raise SystemExit(2)
if input_mode == "dom-smooth" and any(
    report["invalidGestures"] or report["paused"]["anchorReversals"] or report["playing"]["anchorReversals"]
    for report in reports
):
    raise SystemExit("DOM geometry audit failed")
