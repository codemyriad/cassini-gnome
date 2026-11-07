# SPDX-License-Identifier: GPL-3.0-or-later
"""D72: bounded extension decisions; diagnostics never authorize recording."""

import unicodedata


def clean(value, limit):
    return (
        value
        if isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(unicodedata.category(c) == "Cc" for c in value)
        else None
    )


def diagnostics(previous, value):
    if not isinstance(value, dict):
        return dict(previous)
    version = clean(value.get("extensionVersion"), 80)
    instance = clean(value.get("extensionInstance"), 128)
    reset = any(
        new is not None and previous.get(key) is not None and new != previous[key]
        for key, new in (("extensionVersion", version), ("extensionInstance", instance))
    )
    result = {} if reset else dict(previous)
    for key, limit in (("extensionVersion", 80), ("extensionInstance", 128), ("installType", 80)):
        if key in value:
            result[key] = clean(value[key], limit)
    if "filesOk" in value:
        result["filesOk"] = value["filesOk"] if type(value["filesOk"]) is bool else None
    return result


def health(value):
    if not isinstance(value, dict) or value.get("state") not in ("idle", "checking", "ready", "reloadRequired"):
        return None
    rows = value.get("tabs")
    if not isinstance(rows, list) or len(rows) > 100:
        return None
    tabs, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        tab = row.get("tabId")
        if type(tab) is not int or not 0 <= tab <= 9007199254740991 or tab in seen:
            return None
        if row.get("state") not in ("checking", "ready", "reloadRequired"):
            return None
        seen.add(tab)
        tabs.append(
            dict(
                tabId=tab,
                state=row["state"],
                hookSeen=row.get("hookSeen") if type(row.get("hookSeen")) is bool else None,
                relayAlive=row.get("relayAlive") if type(row.get("relayAlive")) is bool else None,
                message=clean(row.get("message"), 2000),
                action="reloadMeetingTab" if row.get("action") == "reloadMeetingTab" else None,
            )
        )
    return dict(
        state=value["state"],
        tabs=tabs,
        worker="ready" if value.get("worker") == "ready" else None,
        nativeControl=value.get("nativeControl")
        if value.get("nativeControl") in ("connected", "disconnected")
        else None,
    )


def reload_tabs(peer):
    value = peer.get("browserHealth")
    if not isinstance(value, dict) or value.get("state") == "idle":
        return ()
    return tuple(tab for tab in value["tabs"] if tab["state"] == "reloadRequired")


def older(version, bundled):
    def parts(value):
        if not isinstance(value, str) or not 1 <= len(value.split(".")) <= 4:
            return None
        fields = value.split(".")
        if any(not field or not field.isascii() or not field.isdecimal() or len(field) > 20 for field in fields):
            return None
        numbers = tuple(int(field) for field in fields)
        return numbers + (0,) * (4 - len(numbers)) if all(n < 2**64 for n in numbers) else None

    a, b = parts(version), parts(bundled)
    return a is not None and b is not None and a < b


def read_peers(value):
    if not isinstance(value, list) or len(value) > 100:
        return ()
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or not (identifier := clean(row.get("id"), 128)) or identifier in seen:
            return ()
        seen.add(identifier)
        caps = row.get("capabilities")
        busy = row.get("busyTabs")
        result.append(
            dict(
                id=identifier,
                browser=clean(row.get("browser"), 100),
                capabilities=tuple(c for c in caps if isinstance(c, str))
                if isinstance(caps, list) and len(caps) <= 16
                else (),
                diagnostics=diagnostics({}, row.get("diagnostics")),
                browserHealth=health(row.get("browserHealth")),
                busyTabs=tuple(busy)
                if isinstance(busy, list) and len(busy) <= 100 and all(type(t) is int for t in busy)
                else None,
            )
        )
    return tuple(result)


def setup_complete(peers):
    # Optional diagnostics: only an observed failure requires file repair.
    # The caller still requires a live, reconciled control connection.
    return all(
        peer["diagnostics"].get("filesOk") is not False and "multi-stream-v1" in peer["capabilities"] for peer in peers
    )


def can_reload(peer, tab):
    return (
        "tab-readiness-v1" in peer["capabilities"]
        and tab["action"] == "reloadMeetingTab"
        and peer["busyTabs"] is not None
        and tab["tabId"] not in peer["busyTabs"]
    )
