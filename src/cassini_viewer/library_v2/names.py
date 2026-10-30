# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §2 Library v2 file names: a port of ux-draft/library-v2/names.py (coordination 1bed4bc).

The functions are the reference implementation verbatim, without its vector generator; the shared
vectors vendored in tests/ux/library-v2/ pin them. The reference notes follow.

A Library file is named `<prefix><title><suffix>.opus`, at most 200 UTF-8 bytes.

prefix   `YYYY-MM-DD--HH.MM-`, the wall-clock minute (floored, 24 h) at the recording
         site at the start instant (S-12 §2 and §11). Sources (prefix_for below), recorded
         as namePrefixSource:
           persisted namePrefix   kept forever, never recomputed;
           recorded               capture start + startedAtUtcOffsetMinutes;
           legacy-device-tz       no offset, or one outside -720..+840 minutes (Q17):
                                  capture start in the Library device's IANA zone at
                                  first conversion;
           date-tag               received: the DATE tag's own wall time when DATE is a
                                  complete timestamp (seconds required) with an explicit
                                  offset (`Z` or `±HH:MM` within -720..+840, never
                                  `-00:00`, RFC 3339's "local offset unknown");
           import-time            received without such a DATE, or an unknown start
                                  (Q16): the conversion/import instant in the device zone.
         recordedAtLocal and mtimes are never used.

title    the full title the Library row shows, sanitized for the name only:
         1. NFC.
         2. Each of these becomes "-" (S-12 §2, §11 Q6a): / \\ : * ? " < > |,
            U+0000-U+001F, U+007F, C1 controls U+0080-U+009F except U+0085 (White_Space),
            bidi controls U+202A-U+202E and U+2066-U+2069, U+FEFF, the noncharacters
            U+FFFE/U+FFFF, and lone surrogates U+D800-U+DFFF. ZWJ and variation
            selectors stay (emoji need them).
         3. Runs of Unicode White_Space become one U+0020; then spaces and dots are
            trimmed at both ends. (Tabs/newlines were already "-" in step 2.)
         3b. Stream-safe (S-12 §11 Q4 revised, UAX #15): a run of more than 30
            consecutive non-starters (canonical combining class != 0) keeps its first 30.
            APFS rejects names with 32 or more (EILSEQ). The result stays NFC.
         4. Empty -> the catalog's untitled fallback (strings.json `library.untitled`,
            in the writing app's language at first publish, §11 Q6b; vectors use "en").

suffix   "", " (2)", " (3)"...: the smallest that gives a free name, ignoring the
         meeting's own current name. "Free" compares the canonical caseless key
         NFC(casefold(NFD(name))) of whole names (S-12 §11 Q5 revised): full case
         folding, so \u00df~ss, \u017f~s, \ufb00~ff and \u03c2~\u03c3~\u03a3. It is a compatibility superset of
         APFS's case/normalization-insensitive matching, not a model of it. Platforms
         implement it with ICU or an equivalent that passes the vectors, never with a
         plain lowercased().
         EEXIST RETRY: the filesystem has the last word. If the no-replace publish
         (renameat2 RENAME_NOREPLACE / renamex_np RENAME_EXCL) fails with EEXIST, the
         caller adds the rejected name to `existing`, takes the next suffix and retries,
         at most 99 retries, then reports an error. file_name() is pure; the retry
         loop belongs to the publisher.

truncate For each candidate suffix, the title is cut to
         200 - len(prefix) - len(suffix) - len(".opus") UTF-8 bytes at a grapheme
         boundary (approximation below), then trailing spaces and dots are trimmed
         again (S-12 §11 Q3). If not even the first cluster fits, the cut falls
         back to a code-point boundary (Q4).

GRAPHEME APPROXIMATION (stdlib has no UAX #29). A boundary exists between two code
points A|B unless one of these holds:
  G1  B is a mark: general category Mn, Mc or Me (combining marks, variation selectors
      U+FE00-FE0F and U+E0100-E01EF, which are Mn);
  G2  B is ZWJ U+200D, or A is ZWJ (emoji ZWJ sequences stay whole);
  G3  B is an emoji modifier U+1F3FB-1F3FF or a tag character U+E0020-E007F;
  G4  A and B are regional indicators U+1F1E6-1F1FF and A is the odd-numbered (1st,
      3rd...) indicator of its run (flags are pairs counted from the run's start);
  G5  Hangul conjoining jamo: B is a V or T jamo, or A is an L jamo and B is an L or V
      jamo or a precomposed syllable U+AC00-D7A3.
Not modelled: Prepend, SpacingMark exceptions beyond Mc, Indic conjunct rules, CR LF
(CR/LF are already "-"). Vectors only use characters assigned in Unicode 8.0 or
earlier, so their categories and NFC are stable across Python 3.9 (Unicode 13) and
later. A platform that uses real UAX #29 clusters agrees on every vector.
"""

import re
import unicodedata
from datetime import UTC, datetime, timedelta

MAX_BYTES = 200
EXTENSION = ".opus"
PREFIX_BYTES = len("YYYY-MM-DD--HH.MM-")
INVISIBLE_FORBIDDEN = (
    {chr(c) for c in range(0x80, 0xA0) if c != 0x85}
    | {chr(c) for c in range(0x202A, 0x202F)}
    | {chr(c) for c in range(0x2066, 0x206A)}
    | {chr(0xFEFF), chr(0xFFFE), chr(0xFFFF)}
    | {chr(c) for c in range(0xD800, 0xE000)}
)
FORBIDDEN = set('/\\:*?"<>|') | {chr(c) for c in range(0x20)} | {"\x7f"} | INVISIBLE_FORBIDDEN
WHITE_SPACE = (
    {chr(c) for c in range(0x09, 0x0E)}
    | {chr(c) for c in (0x20, 0x85, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000)}
    | {chr(c) for c in range(0x2000, 0x200B)}
)
PREFIX_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}--\d{2}\.\d{2}-\Z")
DATE_TAG = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})\Z")


# ---- prefix -------------------------------------------------------------------------------


def parse_utc(value):
    """'2026-10-30T17:02:41.123Z' -> aware UTC datetime (3.9's fromisoformat lacks 'Z')."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,6})?Z", value)
    if not match:
        raise ValueError("start must be RFC 3339 UTC with 'Z': " + value)
    base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    fraction = match.group(2) or ".0"
    return base + timedelta(microseconds=int(fraction[1:].ljust(6, "0")))


def format_prefix(local):
    return local.strftime("%Y-%m-%d--%H.%M-")


def valid_offset(offset_minutes):
    """S-12 §11 Q17: -720..+840 minutes; anything else is an unknown offset."""
    return type(offset_minutes) is int and -720 <= offset_minutes <= 840


def prefix_from_offset(start_utc, offset_minutes):
    return format_prefix(parse_utc(start_utc) + timedelta(minutes=offset_minutes))


def prefix_from_zone(start_utc, zone_name):
    from zoneinfo import ZoneInfo

    local = parse_utc(start_utc).astimezone(ZoneInfo(zone_name))
    return format_prefix(local), int(local.utcoffset().total_seconds() // 60)


def prefix_for(start):
    """Return (namePrefix, utcOffsetMinutes or None, namePrefixSource)."""
    kind = start["kind"]
    if kind == "persisted":
        if not PREFIX_PATTERN.match(start["namePrefix"]):
            raise ValueError("invalid persisted prefix")
        return start["namePrefix"], None, "persisted"
    if kind == "capture":  # a start instant, with or without a usable offset
        offset = start.get("utcOffsetMinutes")
        if valid_offset(offset):
            return prefix_from_offset(start["startUtc"], offset), offset, "recorded"
        prefix, offset = prefix_from_zone(start["startUtc"], start["libraryTimeZone"])
        return prefix, offset, "legacy-device-tz"
    if kind == "unknown-start":  # Q16: salvaged session without a start instant
        prefix, offset = prefix_from_zone(start["importUtc"], start["libraryTimeZone"])
        return prefix, offset, "import-time"
    if kind == "received":
        tag = start.get("dateTag")
        match = DATE_TAG.match(tag) if isinstance(tag, str) else None
        zone = match.group(8) if match else "-00:00"
        offset = 0 if zone == "Z" else int(zone[0] + "1") * (int(zone[1:3]) * 60 + int(zone[4:6]))
        if match and zone != "-00:00" and valid_offset(offset):
            year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
            datetime(year, month, day, hour, minute, second)  # rejects impossible dates
            return f"{year:04d}-{month:02d}-{day:02d}--{hour:02d}.{minute:02d}-", offset, "date-tag"
        prefix, offset = prefix_from_zone(start["importUtc"], start["libraryTimeZone"])
        return prefix, offset, "import-time"
    raise ValueError("unknown start kind " + kind)


# ---- title --------------------------------------------------------------------------------


def sanitize(title, untitled):
    text = unicodedata.normalize("NFC", title)
    text = "".join("-" if c in FORBIDDEN else c for c in text)
    out, pending = [], False
    for c in text:
        if c in WHITE_SPACE:
            pending = True
            continue
        if pending and out:
            out.append(" ")
        pending = False
        out.append(c)
    text = "".join(out).strip(" .")
    return stream_safe(text) or untitled


MAX_NON_STARTERS = 30


def stream_safe(text):
    """Keep at most 30 consecutive non-starters (ccc != 0); drop the rest of each run."""
    out, run = [], 0
    for c in text:
        run = run + 1 if unicodedata.combining(c) else 0
        if run <= MAX_NON_STARTERS:
            out.append(c)
    return "".join(out)


def decode_utf16_units(units):
    """UTF-16 code units -> str; valid pairs combine, unpaired units stay as lone surrogates
    (which sanitize() then turns into "-")."""
    out, i = [], 0
    while i < len(units):
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00)))
            i += 2
        else:
            out.append(chr(u))
            i += 1
    return "".join(out)


def _ri(c):
    return 0x1F1E6 <= ord(c) <= 0x1F1FF


def _jamo(c):
    o = ord(c)
    if 0x1100 <= o <= 0x115F or 0xA960 <= o <= 0xA97C:
        return "L"
    if 0x1160 <= o <= 0x11A7 or 0xD7B0 <= o <= 0xD7C6:
        return "V"
    if 0x11A8 <= o <= 0x11FF or 0xD7CB <= o <= 0xD7FB:
        return "T"
    if 0xAC00 <= o <= 0xD7A3:
        return "S"
    return None


def boundaries(text):
    """Indices i (0 < i < len) where a cluster may end before text[i], per G1-G5."""
    result, ri_run = [], 0
    for i in range(1, len(text)):
        a, b = text[i - 1], text[i]
        ri_run = ri_run + 1 if _ri(a) else 0
        if unicodedata.category(b) in ("Mn", "Mc", "Me"):
            continue
        if b == "\u200d" or a == "\u200d":
            continue
        if 0x1F3FB <= ord(b) <= 0x1F3FF or 0xE0020 <= ord(b) <= 0xE007F:
            continue
        if _ri(a) and _ri(b) and ri_run % 2 == 1:
            continue
        ja, jb = _jamo(a), _jamo(b)
        if jb in ("V", "T") or ja == "L" and jb in ("L", "V", "S"):
            continue
        result.append(i)
    return result


def truncate(text, limit):
    """Longest prefix of whole clusters within `limit` UTF-8 bytes, trailing spaces/dots trimmed."""
    if len(text.encode()) <= limit:
        return text
    cut = 0
    for i in boundaries(text):
        if len(text[:i].encode()) > limit:
            break
        cut = i
    if cut == 0:  # Q4: a single cluster longer than the budget
        cut = max(i for i in range(len(text) + 1) if len(text[:i].encode()) <= limit)
    return text[:cut].rstrip(" .")


# ---- whole name -----------------------------------------------------------------------------


def fold(name):
    """S-12 §11 Q5: the canonical caseless key NFC(casefold(NFD(name)))."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


def file_name(prefix, title, untitled, existing=(), own=None):
    if not PREFIX_PATTERN.match(prefix):
        raise ValueError("invalid prefix")
    clean = sanitize(title, untitled)
    taken = {fold(n) for n in existing}
    if own is not None:
        taken.discard(fold(own))
    n = 1
    while True:
        suffix = "" if n == 1 else f" ({n})"
        budget = MAX_BYTES - PREFIX_BYTES - len(suffix) - len(EXTENSION)
        name = prefix + truncate(clean, budget) + suffix + EXTENSION
        if fold(name) not in taken:
            return name, clean
        n += 1
