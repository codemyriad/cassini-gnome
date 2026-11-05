# SPDX-License-Identifier: GPL-3.0-or-later
"""D58 (R79) classified refusals: sides that were requested but never opened.

The rules are the protocol's (SPEC.md, "Refusals"; conformance/refusals). A
side has opened when its first buffer is admitted within 3 s. Before that,
a failure is a refusal. D58.1: the others' side (computer sound) is never
refused for silence or a missing buffer; it opens on its first buffer however
late, and only an explicit error refuses it. A refusal never changes state,
problem, problemKind, the end reason, othersSource or captureOutcome.
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


# GStreamer's GST_RESOURCE_ERROR_NOT_AUTHORIZED: the system denied the access.
RESOURCE_DOMAIN = "gst-resource-error-quark"
NOT_AUTHORIZED = 15


def gst_code(error):
    """D58.1: the D58 code of a native side's explicit GStreamer error.

    permissionDenied only when the system reports the access as not authorized
    (never inferred from silence or from text); any other failure, a missing
    device or a capture that could not be created, is deviceUnavailable.
    """
    domain, code = getattr(error, "domain", None), getattr(error, "code", None)
    return "permissionDenied" if domain == RESOURCE_DOMAIN and code == NOT_AUTHORIZED else "deviceUnavailable"


class NothingOpened(OSError):
    """Every requested side was refused: no recording, only the start banner."""

    def __init__(self, message, refusals):
        super().__init__(message)
        self.refusals = list(refusals)
        self.unwritten = list(refusals)  # Those not yet stored, should the folder be kept.


class StartCancelled(ValueError):
    """A Stop cancelled every requested side before any opened: no recording,
    and not a refusal (R79 §1), so no banner and no stored refusals."""


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


def banner(refusals, *more):
    """The start banner when nothing opened: its line, captions and action.

    `more` are the refusal lists of further meetings that failed too: the
    banner shows every distinct caption in first-occurrence order (R82
    decision 6). `caption` is the first of them.
    """
    captions = []
    for listed in (refusals, *more):
        first = listed[0] if listed else {}
        caption = CAPTIONS.get(first.get("detail")) if isinstance(first, dict) else None
        if caption is not None and caption not in captions:
            captions.append(caption)
    return dict(
        id="errors.recordFailed.line",
        caption=captions[0] if captions else None,
        captions=captions,
        action="common.tryAgain",
        refusals=list(refusals),
    )


LIVE_KINDS = {
    "me": ("microphone", "local"),
    # The others record from computer sound or from browser audio (R82 1d).
    "others": ("system", "remote", "tab", "media", "application"),
    "browser": ("remote", "tab", "media", "application"),
}


def live_lines(stored, tracks, events=(), armed=None):
    """Recording page lines (R79 §3): one per refused side that records nothing now.

    `tracks` is (kind, ended, startOffsetMs) for each track. A refusal is
    covered (R82 1d) when a track of its side's kinds is live, or started at
    or after the refusal: a side opened later (a turn-on, the callConnected
    seam, a restored browser source) shows no line, and a later loss brings
    back its own line (Why?) rather than the start refusal's caption. A side
    the user turned off after its refusal shows none either. A refused resume
    (resumeRefused) never drives a line: it shows only in Details. Each line
    is keyed on (side, code, detail), never on stored text.

    `armed` maps a track kind to the time of its attempt for a side that is
    selected and waiting for its first buffer (D58.1: the others' side is never
    refused for silence). The manifest cannot say so: Try again writes no
    selection event and the track comes only at the first buffer. An attempt at
    or after a refusal covers it like a track that started then, so waiting
    for sound never shows a refusal; a later explicit error is its own refusal.
    """
    armed = {
        kind: at
        for kind, at in (armed.items() if isinstance(armed, dict) else ())
        if isinstance(kind, str) and isinstance(at, (int, float)) and not isinstance(at, bool)
    }
    selections = [e for e in events if isinstance(e, dict) and e.get("type") == "selection"]
    last = selections[-1] if selections else {}
    latest = {}
    for item in read(stored):
        if item["side"] in LIVE_KINDS and item.get("detail") != "resumeRefused":
            latest.pop(item["side"], None)
            latest[item["side"]] = item
    tracks = [(*track, None)[:3] for track in tracks]
    # An armed side records the others from its device once it hears them.
    live = {kind for kind, ended, _start in tracks if ended is not True} | armed.keys()
    tracks += [(kind, True, at) for kind, at in armed.items()]  # Covers what came before it.
    result = []
    for side, item in latest.items():
        kinds = set(LIVE_KINDS[side])
        if any(
            kind in kinds and (ended is not True or isinstance(start, (int, float)) and start >= item["atMs"])
            for kind, ended, start in tracks
        ):
            continue
        if last.get("me" if side == "me" else "others") is False and last.get("atMs", 0) > item["atMs"]:
            continue  # Turned off after it was refused.
        if side == "me":
            key = "errors.micPermission.line" if item["code"] == "permissionDenied" else "errors.deviceUnavailable.me"
        elif side == "others":
            key = "errors.deviceUnavailable.others"
        else:
            if "system" not in live:
                continue  # Not recorded from computer sound either: its own line says so.
            key = "errors.othersFallback.line"
        result.append(dict(side=side, id=key, caption=CAPTIONS.get(item.get("detail")) if side == "browser" else None))
    return result
