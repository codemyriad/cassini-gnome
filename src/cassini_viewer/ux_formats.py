# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared duration/calendar formats, with locale time style per D37."""

import locale
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _seconds(ms):
    if type(ms) not in (int, float) or not math.isfinite(ms) or ms < 0:
        raise ValueError("Expected nonnegative finite milliseconds")
    return math.floor(ms / 1000)


def position(ms):
    seconds = _seconds(ms)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def live_badge(ms):
    seconds = _seconds(ms)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


live_big = position


def meeting_duration(ms):
    minutes = _seconds(ms) // 60
    if minutes < 1:
        return "under 1 min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def talk_time(ms):
    seconds = _seconds(ms)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def confirmed_speech(ms):
    return meeting_duration(ms) + " confirmed speech"


def local_timezone(identifier):
    """Accept GLib/POSIX identifiers; unsupported zones fall back to UTC."""
    name = identifier.lstrip(":").removeprefix("/usr/share/zoneinfo/") if isinstance(identifier, str) else ""
    try:
        ZoneInfo(name)
        return name
    except (ValueError, KeyError, TypeError, OSError):
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", name)
        if match:
            sign, hours, minutes = match.groups()
            if int(hours) < 24 and int(minutes) < 60:
                offset = timedelta(hours=int(hours), minutes=int(minutes))
                return timezone(offset if sign == "+" else -offset)
        return "UTC"


def _local(at_ms, tz):
    return datetime.fromtimestamp(at_ms / 1000, ZoneInfo(tz) if isinstance(tz, str) else tz)


def _relative_date(at_ms, now_ms, tz):
    at, now = _local(at_ms, tz), _local(now_ms, tz)
    if at.date() == now.date():
        relative = "Today"
    elif at.date() == now.date() - timedelta(days=1):
        relative = "Yesterday"
    else:
        relative = None
    return at, now, relative


def day_group(at_ms, *, now_ms, tz):
    if at_ms is None:
        return "Date unknown"
    at, now, relative = _relative_date(at_ms, now_ms, tz)
    if relative:
        return relative
    year = f" {at.year}" if at.year != now.year else ""
    return f"{WEEKDAYS[at.weekday()]} {at.day} {MONTHS[at.month - 1]}{year}"


def row_time(at_ms, *, tz):
    if at_ms is None:
        return ""
    at = _local(at_ms, tz)
    try:
        pattern = locale.nl_langinfo(locale.T_FMT)
    except (AttributeError, ValueError):
        pattern = "%H:%M"
    if any(token in pattern for token in ("%I", "%l", "%r")) and "%H" not in pattern:
        return at.strftime("%I:%M %p").lstrip("0")
    return f"{at.hour:02d}:{at.minute:02d}"


def preview_subtitle(at_ms, duration_ms, *, now_ms, tz, received=False):
    value = day_group(at_ms, now_ms=now_ms, tz=tz)
    if at_ms is not None:
        value += f", {row_time(at_ms, tz=tz)}"
    if duration_ms is not None:
        value += f" · {meeting_duration(duration_ms)}"
    return value + (" · Received" if received else "")


def search_date(at_ms, *, now_ms, tz):
    if at_ms is None:
        return day_group(at_ms, now_ms=now_ms, tz=tz)
    at, now, relative = _relative_date(at_ms, now_ms, tz)
    if relative:
        return relative
    year = f" {at.year}" if at.year != now.year else ""
    return f"{at.day} {MONTHS[at.month - 1]}{year}"


def search_context(at_ms, duration_ms, position_ms, *, now_ms, tz):
    return preview_subtitle(at_ms, duration_ms, now_ms=now_ms, tz=tz) + f" · at {position(position_ms)}"


def row_meta(duration_ms, names):
    names = tuple(names)
    value = meeting_duration(duration_ms) if duration_ms is not None else ""
    if names:
        value += (" · " if value else "") + ", ".join(names[:3])
        if len(names) > 3:
            value += f" +{len(names) - 3}"
    return value or None


def eta(minutes):
    if minutes is None:
        return "Transcribing…"
    if type(minutes) is not int or minutes < 0:
        raise ValueError("Expected nonnegative integer minutes")
    if minutes == 0:
        return "Transcribing, less than a minute left"
    return f"Transcribing, about {minutes} " + ("minute left" if minutes == 1 else "minutes left")


def search_count(places, meetings):
    return (
        f"{places} "
        + ("place" if places == 1 else "places")
        + f" in {meetings} "
        + ("meeting" if meetings == 1 else "meetings")
    )


def size(byte_count, *, approx=False):
    if type(byte_count) is not int or byte_count < 0:
        raise ValueError("Expected nonnegative integer bytes")
    if byte_count < 1048576:
        from .strings import text

        count = (Decimal(byte_count) / 1024).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return ("about " if approx else "") + text("format.size.kb", n=count)
    count = (Decimal(byte_count) / 1048576).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if count < 1000:
        value = f"{count} MB"
    else:
        count = (Decimal(byte_count) / 1073741824).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        value = f"{count} GB"
    return ("about " if approx else "") + value
