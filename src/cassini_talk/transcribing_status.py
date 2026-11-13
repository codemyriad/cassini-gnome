# SPDX-License-Identifier: GPL-3.0-or-later
"""WP5 Transcribing header status line and details popover (L-45, L-48)."""

import math
from dataclasses import dataclass
from functools import cache

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import ux_formats as fmt  # noqa: E402
from .strings import plural, text  # noqa: E402
from .tokens import token  # noqa: E402

STEPS: tuple[tuple[str, str], ...] = (
    ("reading", "transcribing.step.reading"),
    ("preparing", "transcribing.step.preparing"),
    ("words", "transcribing.step.words"),
    ("speakers", "transcribing.step.speakers"),
    ("saving", "transcribing.step.saving"),
)
STEP_INDEX: dict[str, int] = {key: index for index, (key, _sid) in enumerate(STEPS)}


@dataclass(frozen=True)
class ResolvedTranscribingState:
    step: str
    percent: int
    fraction: float
    eta_minutes: int | None
    audio_done_ms: int
    total_ms: int
    words_so_far: int


def resolve_transcribing_state(meeting, live_progress=None) -> ResolvedTranscribingState:
    total_ms = max(0, int(meeting.duration_ms or 0))
    word_count = len(meeting.words)
    if live_progress is not None:
        step = live_progress.step if live_progress.step in STEP_INDEX else "reading"
        eta_minutes = live_progress.eta_minutes if live_progress.eta_minutes is not None else meeting.eta_minutes
        words_so_far = max(word_count, max(0, int(live_progress.words_so_far)))
        raw_done = max(0, int(live_progress.audio_done_ms))
        audio_done_ms = min(total_ms, raw_done) if total_ms > 0 else raw_done
        fraction = min(0.99, max(0.0, float(live_progress.fraction)))
        if fraction == 0.0 and total_ms > 0 and audio_done_ms > 0:
            fraction = min(0.99, audio_done_ms / total_ms)
    else:
        eta_minutes = meeting.eta_minutes
        words_so_far = word_count
        max_word_end = max((w.end_ms for w in meeting.words), default=0)
        audio_done_ms = min(total_ms, max(0, max_word_end)) if total_ms > 0 else max(0, max_word_end)
        if meeting.transcript == "queued":
            step = "reading"
        else:
            step = "words" if (words_so_far > 0 or eta_minutes is not None) else "reading"
        fraction = min(0.99, max(0.0, audio_done_ms / total_ms)) if total_ms > 0 and audio_done_ms > 0 else 0.0
    percent = min(99, max(0, int(round(fraction * 100))))
    return ResolvedTranscribingState(
        step=step,
        percent=percent,
        fraction=fraction,
        eta_minutes=eta_minutes,
        audio_done_ms=audio_done_ms,
        total_ms=total_ms,
        words_so_far=words_so_far,
    )


def status_line_parts(eta_minutes: int | None) -> tuple[str, str | None]:
    if eta_minutes is None:
        return text("states.badge.transcribing.noEta"), None
    if eta_minutes == 0:
        return text("states.badge.transcribing.underMinute"), None
    return text("transcribing.heading"), "· " + plural("transcribing.eta.minutes", eta_minutes)


def popover_stat_values(state: ResolvedTranscribingState) -> dict[str, str]:
    if state.eta_minutes is None:
        time_left = "—"
    elif state.eta_minutes == 0:
        time_left = text("format.meetingDuration.underMinute")
    else:
        time_left = plural("transcribing.stat.timeLeftValue", state.eta_minutes)
    return {
        "percent": f"{fmt.number(state.percent)}%",
        "time_left": time_left,
        "audio_done": text(
            "transcribing.stat.audioDoneValue",
            done=fmt.live_badge(state.audio_done_ms),
            total=fmt.live_badge(state.total_ms),
        ),
        "words_so_far": fmt.number(state.words_so_far),
    }


def _rgba(role: str, *, dark: bool, alpha: float = 1.0) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(token(role, dark=dark))
    color.alpha *= alpha
    return color


@cache
def _vector_primitives() -> dict[str, object]:
    builder_24 = Gsk.PathBuilder.new()
    builder_24.add_circle(Graphene.Point().init(12.0, 12.0), 9.0)
    spin_ring = builder_24.to_path()
    spin_arc = Gsk.Path.parse("M12 3a9 9 0 0 1 9 9")

    builder_done = Gsk.PathBuilder.new()
    builder_done.add_circle(Graphene.Point().init(8.0, 8.0), 8.0)
    done_circle = builder_done.to_path()
    done_tick = Gsk.Path.parse("M4.6 8.3l2.2 2.2 4.6-4.9")

    builder_up = Gsk.PathBuilder.new()
    builder_up.add_circle(Graphene.Point().init(8.0, 8.0), 7.0)
    upcoming_ring = builder_up.to_path()

    chevron = Gsk.Path.parse("M1.5 1.5l5 5-5 5")

    stroke_3 = Gsk.Stroke.new(3.0)
    stroke_3_round = Gsk.Stroke.new(3.0)
    stroke_3_round.set_line_cap(Gsk.LineCap.ROUND)

    stroke_tick = Gsk.Stroke.new(1.8)
    stroke_tick.set_line_cap(Gsk.LineCap.ROUND)
    stroke_tick.set_line_join(Gsk.LineJoin.ROUND)

    stroke_2 = Gsk.Stroke.new(2.0)

    stroke_chevron = Gsk.Stroke.new(1.8)
    stroke_chevron.set_line_cap(Gsk.LineCap.ROUND)
    stroke_chevron.set_line_join(Gsk.LineJoin.ROUND)

    assert spin_arc is not None and done_tick is not None and chevron is not None
    return {
        "spin_ring": spin_ring,
        "spin_arc": spin_arc,
        "stroke_3": stroke_3,
        "stroke_3_round": stroke_3_round,
        "done_circle": done_circle,
        "done_tick": done_tick,
        "stroke_tick": stroke_tick,
        "upcoming_ring": upcoming_ring,
        "stroke_2": stroke_2,
        "chevron": chevron,
        "stroke_chevron": stroke_chevron,
    }


def snapshot_spinner(snapshot: Gtk.Snapshot, size_px: int, angle_deg: float = 0.0, *, dark: bool = False) -> None:
    if size_px <= 0:
        return
    prims = _vector_primitives()
    snapshot.save()
    scale = size_px / 24.0
    snapshot.scale(scale, scale)
    snapshot.append_stroke(prims["spin_ring"], prims["stroke_3"], _rgba("line", dark=dark))
    if angle_deg:
        snapshot.translate(Graphene.Point().init(12.0, 12.0))
        snapshot.rotate(angle_deg)
        snapshot.translate(Graphene.Point().init(-12.0, -12.0))
    snapshot.append_stroke(prims["spin_arc"], prims["stroke_3_round"], _rgba("accent", dark=dark))
    snapshot.restore()


def snapshot_step_indicator(snapshot: Gtk.Snapshot, state: str, *, angle_deg: float = 0.0, dark: bool = False) -> None:
    prims = _vector_primitives()
    if state == "done":
        snapshot.append_fill(prims["done_circle"], Gsk.FillRule.WINDING, _rgba("accent", dark=dark))
        snapshot.append_stroke(prims["done_tick"], prims["stroke_tick"], _rgba("on-accent", dark=dark))
    elif state == "active":
        snapshot_spinner(snapshot, 16, angle_deg, dark=dark)
    else:
        snapshot.append_stroke(prims["upcoming_ring"], prims["stroke_2"], _rgba("line", dark=dark))


def snapshot_chevron(snapshot: Gtk.Snapshot, *, dark: bool = False) -> None:
    prims = _vector_primitives()
    snapshot.save()
    snapshot.scale(7.0 / 8.0, 11.0 / 13.0)
    snapshot.append_stroke(prims["chevron"], prims["stroke_chevron"], _rgba("ink-3", dark=dark))
    snapshot.restore()


def snapshot_progress_bar(
    snapshot: Gtk.Snapshot,
    width: float,
    height: float,
    fraction: float,
    *,
    radius_px: float = 2.0,
    dark: bool = False,
) -> None:
    if width <= 0 or height <= 0:
        return
    rect = Graphene.Rect().init(0.0, 0.0, float(width), float(height))
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(rect, float(radius_px))
    snapshot.push_rounded_clip(rounded)
    snapshot.append_color(_rgba("ink", dark=dark, alpha=0.10), rect)
    clamped = min(1.0, max(0.0, float(fraction)))
    if clamped > 0.0:
        fill_w = min(float(width), max(float(height), float(width) * clamped))
        snapshot.append_color(_rgba("accent", dark=dark), Graphene.Rect().init(0.0, 0.0, fill_w, float(height)))
    snapshot.pop()


class _AnimatedSpinnerMixin:
    def _init_animation(self) -> None:
        self._tick_id = 0
        self._start_us = 0
        self.connect("map", self._sync_spin_tick)
        self.connect("unmap", self._stop_spin_tick)

    def _animations_enabled(self) -> bool:
        settings = Gtk.Settings.get_default()
        return bool(settings and settings.props.gtk_enable_animations)

    def _should_spin(self) -> bool:
        return True

    def _angle_deg(self) -> float:
        if not self._tick_id or not self._animations_enabled():
            return 0.0
        elapsed_s = max(0.0, (GLib.get_monotonic_time() - self._start_us) / 1_000_000.0)
        return math.fmod(elapsed_s * 360.0, 360.0)

    def _sync_spin_tick(self, *_args) -> None:
        if self.get_mapped() and self._should_spin() and self._animations_enabled():
            if not self._tick_id:
                self._start_us = GLib.get_monotonic_time()
                self._tick_id = self.add_tick_callback(self._on_spin_tick)
        else:
            self._stop_spin_tick()
            self.queue_draw()

    def _stop_spin_tick(self, *_args) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_spin_tick(self, _widget, _clock) -> bool:
        if not self.get_mapped() or not self._should_spin() or not self._animations_enabled():
            self._tick_id = 0
            self.queue_draw()
            return GLib.SOURCE_REMOVE
        self.queue_draw()
        return GLib.SOURCE_CONTINUE


class AccentSpinner(_AnimatedSpinnerMixin, Gtk.Widget):
    """Accent arc spinner matching Transcribing-GNOME.dc.html."""

    def __init__(self, size_px: int = 14):
        super().__init__(
            can_target=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self.size_px = int(size_px)
        self._init_animation()

    def do_measure(self, _orientation, _for_size):
        return (self.size_px, self.size_px, -1, -1)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot.save()
        if w != self.size_px or h != self.size_px:
            snapshot.translate(Graphene.Point().init((w - self.size_px) / 2.0, (h - self.size_px) / 2.0))
        snapshot_spinner(snapshot, self.size_px, self._angle_deg(), dark=dark)
        snapshot.restore()


class StepIndicator(_AnimatedSpinnerMixin, Gtk.Widget):
    """16×16 done / active / upcoming step indicator in the details popover."""

    def __init__(self, state: str = "upcoming"):
        super().__init__(
            can_target=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self.state = state
        self._init_animation()

    def _should_spin(self) -> bool:
        return self.state == "active"

    def set_state(self, state: str) -> None:
        if self.state != state:
            self.state = state
            self._sync_spin_tick()
            self.queue_draw()

    def do_measure(self, _orientation, _for_size):
        return (16, 16, -1, -1)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot.save()
        if w != 16 or h != 16:
            snapshot.translate(Graphene.Point().init((w - 16) / 2.0, (h - 16) / 2.0))
        snapshot_step_indicator(snapshot, self.state, angle_deg=self._angle_deg(), dark=dark)
        snapshot.restore()


class ChevronGlyph(Gtk.Widget):
    """7×11 right chevron matching Transcribing-GNOME.dc.html."""

    def __init__(self):
        super().__init__(
            can_target=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )

    def do_measure(self, orientation, _for_size):
        size = 7 if orientation == Gtk.Orientation.HORIZONTAL else 11
        return (size, size, -1, -1)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot.save()
        if w != 7 or h != 11:
            snapshot.translate(Graphene.Point().init((w - 7) / 2.0, (h - 11) / 2.0))
        snapshot_chevron(snapshot, dark=dark)
        snapshot.restore()


class ThinProgressBar(Gtk.Widget):
    """Thin rounded progress bar painted in 10%-alpha ink track and --cx-accent fill."""

    def __init__(self, *, width_px: int = -1, height_px: int = 4, radius_px: float = 2.0, hexpand: bool = False):
        super().__init__(
            can_target=False,
            hexpand=hexpand,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self.width_px = int(width_px)
        self.height_px = int(height_px)
        self.radius_px = float(radius_px)
        self.fraction = 0.0
        if self.width_px > 0:
            self.set_size_request(self.width_px, self.height_px)
        else:
            self.set_size_request(-1, self.height_px)

    def set_fraction(self, fraction: float) -> None:
        clamped = min(1.0, max(0.0, float(fraction)))
        if abs(clamped - self.fraction) > 1e-4:
            self.fraction = clamped
            self.queue_draw()

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            w = self.width_px if self.width_px > 0 else 120
            return (w, w, -1, -1)
        return (self.height_px, self.height_px, -1, -1)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot_progress_bar(snapshot, float(w), float(h), self.fraction, radius_px=self.radius_px, dark=dark)


class TranscribingStatusRow(Gtk.Box):
    """Header status line + chevron details popover for a transcribing meeting."""

    def __init__(self, *, on_stop):
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            valign=Gtk.Align.CENTER,
            margin_top=2,
            css_classes=["transcribing-status-row"],
            visible=False,
        )
        self._on_stop = on_stop
        self._last_render_key = None

        self.popover = Gtk.Popover(
            has_arrow=True,
            position=Gtk.PositionType.BOTTOM,
            halign=Gtk.Align.START,
            css_classes=["transcribing-popover"],
        )
        self.popover.update_property([Gtk.AccessibleProperty.LABEL], [text("transcribing.details.a11y")])
        self._build_popover_body()

        trigger_content = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.spinner = AccentSpinner(14)
        trigger_content.append(self.spinner)

        text_box = Gtk.Box(spacing=5, valign=Gtk.Align.CENTER)
        self.heading_label = Gtk.Label(
            label=text("transcribing.heading"),
            xalign=0,
            css_classes=["transcribing-status-heading"],
        )
        self.eta_label = Gtk.Label(
            label="",
            xalign=0,
            visible=False,
            css_classes=["transcribing-status-eta"],
        )
        text_box.append(self.heading_label)
        text_box.append(self.eta_label)
        trigger_content.append(text_box)

        self.chevron = ChevronGlyph()
        trigger_content.append(self.chevron)

        a11y_label = text("transcribing.details.a11y")
        self.details_button = Gtk.MenuButton(
            child=trigger_content,
            popover=self.popover,
            tooltip_text=a11y_label,
            css_classes=["flat", "transcribing-details-btn"],
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.GROUP,
        )
        self.details_button.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])
        inner_toggle = self.details_button.get_first_child()
        if inner_toggle is not None:
            inner_toggle.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])
        self.append(self.details_button)

        self.progress_bar = ThinProgressBar(width_px=120, height_px=4, radius_px=2.0)
        self.append(self.progress_bar)

        self.stop_button = Gtk.Button(
            label=text("common.stop"),
            css_classes=["flat", "transcribing-stop-link"],
            valign=Gtk.Align.CENTER,
            margin_start=2,
        )
        self.stop_button.connect("clicked", lambda *_: self._trigger_stop())
        self.append(self.stop_button)

    def _build_popover_body(self) -> None:
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            width_request=284,
            css_classes=["transcribing-popover-body"],
        )

        header_block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title_row = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.popover_title = Gtk.Label(
            label=text("transcribing.heading"),
            xalign=0,
            hexpand=True,
            css_classes=["transcribing-popover-title"],
        )
        self.popover_pct = Gtk.Label(
            label="0%",
            xalign=1,
            css_classes=["transcribing-popover-pct"],
        )
        title_row.append(self.popover_title)
        title_row.append(self.popover_pct)
        header_block.append(title_row)

        self.popover_bar = ThinProgressBar(height_px=5, radius_px=2.5, hexpand=True)
        header_block.append(self.popover_bar)
        body.append(header_block)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.step_rows: list[tuple[str, StepIndicator, Gtk.Label]] = []
        for step_key, string_id in STEPS:
            row = Gtk.Box(spacing=9, valign=Gtk.Align.CENTER)
            indicator = StepIndicator("upcoming")
            lbl = Gtk.Label(label=text(string_id), xalign=0, css_classes=["transcribing-step-upcoming"])
            row.append(indicator)
            row.append(lbl)
            steps_box.append(row)
            self.step_rows.append((step_key, indicator, lbl))
        body.append(steps_box)

        grid = Gtk.Grid(
            column_spacing=14,
            row_spacing=10,
            column_homogeneous=True,
            css_classes=["transcribing-stats-grid"],
        )
        self.stat_time_left = self._stat_cell(grid, 0, 0, "transcribing.stat.timeLeft")
        self.stat_audio_done = self._stat_cell(grid, 1, 0, "transcribing.stat.audioDone")
        self.stat_words_so_far = self._stat_cell(grid, 0, 1, "transcribing.stat.wordsSoFar")
        body.append(grid)

        self.popover_stop_button = Gtk.Button(
            label=text("transcribing.stopAndKeepProgress"),
            hexpand=True,
            css_classes=["flat", "transcribing-popover-stop"],
        )
        self.popover_stop_button.connect("clicked", lambda *_: self._trigger_stop())
        body.append(self.popover_stop_button)

        self.popover_note = Gtk.Label(
            label=text("transcribing.backgroundNote"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            xalign=0.5,
            max_width_chars=42,
            css_classes=["transcribing-popover-note"],
        )
        body.append(self.popover_note)
        self.popover.set_child(body)

    def _stat_cell(self, grid: Gtk.Grid, col: int, row: int, label_id: str) -> Gtk.Label:
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        caption = Gtk.Label(label=text(label_id), xalign=0, css_classes=["transcribing-stat-label"])
        value = Gtk.Label(label="", xalign=0, css_classes=["transcribing-stat-value"])
        cell.append(caption)
        cell.append(value)
        grid.attach(cell, col, row, 1, 1)
        return value

    def _trigger_stop(self) -> None:
        self.popdown()
        self._on_stop()

    def popdown(self) -> None:
        if self.popover.get_mapped() or self.popover.get_visible():
            self.popover.popdown()

    def invalidate_theme(self) -> None:
        self.spinner.queue_draw()
        self.chevron.queue_draw()
        self.progress_bar.queue_draw()
        self.popover_bar.queue_draw()
        for _key, indicator, _lbl in self.step_rows:
            indicator.queue_draw()

    def update_state(
        self,
        meeting,
        *,
        live_progress=None,
        can_stop: bool = True,
        stopping: bool = False,
        lang: str = "en",
    ) -> None:
        state = resolve_transcribing_state(meeting, live_progress)
        render_key = (state, bool(can_stop), bool(stopping), lang)
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key

        heading_text, eta_suffix = status_line_parts(state.eta_minutes)
        self.heading_label.set_label(heading_text)
        self.eta_label.set_visible(eta_suffix is not None)
        if eta_suffix is not None:
            self.eta_label.set_label(eta_suffix)

        a11y_label = text("transcribing.details.a11y")
        self.details_button.set_tooltip_text(a11y_label)
        self.details_button.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])
        inner_toggle = self.details_button.get_first_child()
        if inner_toggle is not None:
            inner_toggle.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])
        self.popover.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])

        self.progress_bar.set_fraction(state.fraction)
        self.popover_bar.set_fraction(state.fraction)

        self.stop_button.set_label(text("common.stop"))
        self.stop_button.set_visible(can_stop)
        self.stop_button.set_sensitive(not stopping)

        self.popover_title.set_label(text("transcribing.heading"))
        stats = popover_stat_values(state)
        self.popover_pct.set_label(stats["percent"])
        self.stat_time_left.set_label(stats["time_left"])
        self.stat_audio_done.set_label(stats["audio_done"])
        self.stat_words_so_far.set_label(stats["words_so_far"])

        active_idx = STEP_INDEX.get(state.step, 0)
        for idx, (_step_key, indicator, lbl) in enumerate(self.step_rows):
            lbl.set_label(text(STEPS[idx][1]))
            for cls in ("transcribing-step-done", "transcribing-step-active", "transcribing-step-upcoming"):
                lbl.remove_css_class(cls)
            if idx < active_idx:
                indicator.set_state("done")
                lbl.add_css_class("transcribing-step-done")
            elif idx == active_idx:
                indicator.set_state("active")
                lbl.add_css_class("transcribing-step-active")
            else:
                indicator.set_state("upcoming")
                lbl.add_css_class("transcribing-step-upcoming")

        self.popover_stop_button.set_label(text("transcribing.stopAndKeepProgress"))
        self.popover_stop_button.set_visible(can_stop)
        self.popover_stop_button.set_sensitive(not stopping)
        self.popover_note.set_label(text("transcribing.backgroundNote"))
