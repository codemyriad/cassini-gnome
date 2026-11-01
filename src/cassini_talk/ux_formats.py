# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared duration/calendar formats, with locale time style per D37."""

import locale
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from . import calendar_formats
from .i18n import number
from .strings import plural, text


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
        return text("format.meetingDuration.underMinute")
    hours, minutes = divmod(minutes, 60)
    return (
        text("format.meetingDuration.hours", h=hours, mm=f"{minutes:02d}")
        if hours
        else text("format.meetingDuration.minutes", m=minutes)
    )


def talk_time(ms):
    seconds = _seconds(ms)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return text("format.meetingDuration.hours", h=hours, mm=f"{minutes:02d}")
    return text("format.talkTime", m=minutes, s=seconds) if minutes else f"{seconds} s"


def confirmed_speech(ms):
    return text("format.confirmedSpeech", duration=meeting_duration(ms))


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
        relative = text("format.today")
    elif at.date() == now.date() - timedelta(days=1):
        relative = text("format.yesterday")
    else:
        relative = None
    return at, now, relative


def day_group(at_ms, *, now_ms, tz):
    if at_ms is None:
        return text("library.group.unknownDate")
    at, now, relative = _relative_date(at_ms, now_ms, tz)
    if relative:
        return relative
    year = f" {at.year}" if at.year != now.year else ""
    return (
        text(
            "format.dayGroup", weekday=calendar_formats.weekday(at), dayOfMonth=at.day, month=calendar_formats.month(at)
        )
        + year
    )


def row_time(at_ms, *, tz):
    if at_ms is None:
        return ""
    at = _local(at_ms, tz)
    try:
        pattern = locale.nl_langinfo(locale.T_FMT)
        language = locale.getlocale(locale.LC_TIME)[0] or "C"
    except (AttributeError, ValueError):
        pattern, language = "%H:%M", "C"
    # musl accepts locale names but supplies the C LC_TIME template. Honour
    # the US clock style even when those locale tables are unavailable.
    us_fallback = pattern == "%H:%M:%S" and language.rsplit("_", 1)[-1] == "US"
    if us_fallback or (any(token in pattern for token in ("%I", "%l", "%r")) and "%H" not in pattern):
        return at.strftime("%I:%M %p").lstrip("0")
    return f"{at.hour:02d}:{at.minute:02d}"


def preview_subtitle(at_ms, duration_ms, *, now_ms, tz, received=False):
    value = day_group(at_ms, now_ms=now_ms, tz=tz)
    if at_ms is not None:
        value += f", {row_time(at_ms, tz=tz)}"
    if duration_ms is not None:
        value += f" · {meeting_duration(duration_ms)}"
    return value + (" · " + text("states.badge.received") if received else "")


def search_date(at_ms, *, now_ms, tz):
    if at_ms is None:
        return day_group(at_ms, now_ms=now_ms, tz=tz)
    at, now, relative = _relative_date(at_ms, now_ms, tz)
    if relative:
        return relative
    year = f" {at.year}" if at.year != now.year else ""
    return text("format.searchDate", dayOfMonth=at.day, month=calendar_formats.month(at)) + year


def search_context(at_ms, duration_ms, position_ms, *, now_ms, tz):
    if at_ms is not None and duration_ms is not None:
        return text(
            "format.searchContext",
            day=day_group(at_ms, now_ms=now_ms, tz=tz),
            time=row_time(at_ms, tz=tz),
            duration=meeting_duration(duration_ms),
            position=position(position_ms),
        )
    # Omit an unknown start or duration as preview_subtitle() does, keeping
    # the catalog's localized "at {position}" ending.
    marker = "\0"
    ending = text("format.searchContext", day="", time="", duration=marker, position=position(position_ms))
    return preview_subtitle(at_ms, duration_ms, now_ms=now_ms, tz=tz) + " · " + ending.split(marker + " · ", 1)[1]


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
        return text("states.badge.transcribing.noEta")
    if type(minutes) is not int or minutes < 0:
        raise ValueError("Expected nonnegative integer minutes")
    if minutes == 0:
        return text("states.badge.transcribing.underMinute")
    return plural("states.badge.transcribing", minutes)


def search_count(places, meetings):
    return text(
        "format.searchCount.join",
        places=plural("format.searchCount.places", places),
        meetings=plural("format.searchCount.meetings", meetings),
    )


def size(byte_count, *, approx=False):
    if type(byte_count) is not int or byte_count < 0:
        raise ValueError("Expected nonnegative integer bytes")
    if byte_count < 1048576:
        count = (Decimal(byte_count) / 1024).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if count < 1000:
            count = max(count, 1) if byte_count else count
            value = text("format.size.kb", n=count)
            return text("format.size.approx", size=value) if approx else value
    count = (Decimal(byte_count) / 1048576).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if count < 1000:
        value = text("format.size", n=count)
    else:
        count = (Decimal(byte_count) / 1073741824).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        from .i18n import gettext as _

        value = _("{n} GB").format(n=number(count))
    return text("format.size.approx", size=value) if approx else value


def full_date_time(at_ms, *, tz):
    """UI-language CLDR full date, with the formatting locale's short clock."""
    at = _local(at_ms, tz)
    return calendar_formats.full_date(at) + ", " + row_time(at_ms, tz=tz)
