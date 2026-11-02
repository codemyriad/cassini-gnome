# SPDX-License-Identifier: GPL-3.0-or-later
"""D58 (R79) classified refusals: sides that were requested but never opened.

The rules are the protocol's (SPEC.md, "Refusals"; conformance/refusals). A
side has opened when its first buffer is admitted within 3 s. Before that,
a failure is a refusal. A refusal never changes state, problem, problemKind,
the end reason, othersSource or captureOutcome.
"""

import re

WORD = re.compile(r"[A-Za-z]{1,32}")  # Always fullmatch: `$` also matches before a final newline.
CAP = 16
MESSAGE = 200
MAX_MS = 86460000
SIDES = {"microphone": "me", "system": "others"}
SOURCES = {"me": "details.source.microphone", "others": "details.source.system", "browser": "details.source.browser"}
ROLE = {"me": "me", "others": "others", "browser": "others"}  # Browser audio is The others' audio.
REFUSED = ("permissionDenied", "deviceUnavailable", "startRejected")
# Banner captions for a first refusal's detail (R79 §3); any other detail adds none.
CAPTIONS = {
    "busy": "errors.refusal.busy",
    "noSources": "errors.refusal.noSources",
    "timeout": "errors.refusal.noAnswer",
    "disconnected": "errors.refusal.noAnswer",
}


def word(value):
    return isinstance(value, str) and WORD.fullmatch(value) is not None


def entry(at_ms, side, code, *, detail=None, message=None):
    """One stored entry with only the fields given, in protocol order."""
    item = {"atMs": min(max(0, at_ms), MAX_MS), "side": side, "code": code}
    if word(detail):
        item["detail"] = detail
    if isinstance(message, str) and message:
        item["message"] = message[:MESSAGE]  # Python strings index Unicode code points.
    return item


def append(manifest, item):
    """Keep the first 15 plus the latest; the member exists only when non-empty."""
    stored = manifest.setdefault("refusals", [])
    if len(stored) < CAP:
        stored.append(item)
    else:
        stored[-1] = item


def from_result(result, at_ms=0):
    """The browser refusal an ok:false Start result records."""
    result = result if isinstance(result, dict) else {}
    return entry(at_ms, "browser", "startRejected", detail=result.get("code"), message=result.get("error"))


def gst_message(error, debug):
    """A GStreamer error's domain, code and debug string: raw, never translated."""
    domain = getattr(error, "domain", None)
    head = f"{domain}:{getattr(error, 'code', '')}" if domain else ""
    return ": ".join(part for part in (head, debug or "") if part) or getattr(error, "message", "") or ""


class NothingOpened(OSError):
    """Every requested side was refused: no recording, only the start banner."""

    def __init__(self, message, refusals):
        super().__init__(message)
        self.refusals = list(refusals)


def well_formed(item):
    """An entry the session schema accepts. A null detail or message means absent."""
    if not isinstance(item, dict):
        return False
    at = item.get("atMs")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at <= MAX_MS:
        return False
    if not word(item.get("side")) or not word(item.get("code")):
        return False
    if item.get("detail") is not None and not word(item["detail"]):
        return False
    message = item.get("message")
    return message is None or isinstance(message, str) and len(message) <= MESSAGE


def read(value):
    """Lenient reading: a non-list member is ignored whole, a malformed entry skipped."""
    return [item for item in value if well_formed(item)] if isinstance(value, list) else []


def line(item):
    """The Audio sources line of one readable entry, as (catalog id, source id), or None."""
    if item["side"] not in SOURCES:
        return None  # An unknown side is not shown.
    known = item["code"] in REFUSED  # Anything else, startFailed included, is shown as failed.
    return ("details.sources.refused." + item["code"] if known else "details.sources.failed", SOURCES[item["side"]])


def lines(stored):
    """Distinct refusal lines in first-occurrence order, and the roles they cover."""
    shown, roles = [], set()
    for item in read(stored):
        key = line(item)
        if key is None:
            continue
        roles.add(ROLE[item["side"]])
        if key not in shown:
            shown.append(key)
    return shown, roles


def banner(refusals):
    """The start banner when nothing opened: its line, caption and action."""
    first = refusals[0] if refusals else {}
    return dict(
        id="errors.recordFailed.line",
        caption=CAPTIONS.get(first.get("detail")),
        action="common.tryAgain",
        refusals=list(refusals),
    )


LIVE_KINDS = {
    "me": ("microphone", "local"),
    "others": ("system",),
    "browser": ("remote", "tab", "media", "application"),
}


def live_lines(stored, tracks, events=()):
    """Recording page lines (R79 §3): one per refused side that records nothing now.

    `tracks` is (kind, ended) for each track. A side that opened later (a
    successful turn-on, a restored browser source), or that the user turned
    off after its refusal, shows no line. Each line is keyed on (side, code,
    detail), never on stored text.
    """
    selections = [e for e in events if isinstance(e, dict) and e.get("type") == "selection"]
    last = selections[-1] if selections else {}
    latest = {}
    for item in read(stored):
        if item["side"] in LIVE_KINDS:
            latest.pop(item["side"], None)
            latest[item["side"]] = item
    live = {kind for kind, ended in tracks if ended is not True}
    result = []
    for side, item in latest.items():
        if live & set(LIVE_KINDS[side]):
            continue
        if last.get("me" if side == "me" else "others") is False and last.get("atMs", 0) > item["atMs"]:
            continue  # Turned off after it was refused.
        if side == "me":
            key = "errors.micPermission.line" if item["code"] == "permissionDenied" else "errors.deviceUnavailable.me"
        elif side == "others":
            key = "errors.deviceUnavailable.others"
        else:
            key = "errors.othersFallback.line"
        result.append(dict(side=side, id=key, caption=CAPTIONS.get(item.get("detail")) if side == "browser" else None))
    return result
