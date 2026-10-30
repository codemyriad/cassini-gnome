# SPDX-License-Identifier: GPL-3.0-or-later
"""x["codemyriad.io"] (schema 1): the vendored x-schema.json and the MAPPING.md split.

valid() checks a value against x-schema.json without a runtime dependency. Its
`$ref`s point into ../meeting.schema.json and are checked by library_schema.valid,
the app's validator for that schema; the extra vocabulary x-schema.json itself
uses ($ref, propertyNames, dependentRequired, minItems) is handled here. Unknown
keywords fail closed, as in library_schema.
"""

import json
from copy import deepcopy
from pathlib import Path

from .. import library_schema

SCHEMA = json.loads(Path(__file__).with_name("x-schema.json").read_text())
MEETING_SCHEMA = library_schema.SCHEMA
DOMAIN = "codemyriad.io"
NOTES_DOMAIN = "gocassini.com"
# x.meeting holds exactly these meeting.json members (MAPPING.md); the x top level
# holds id/revision/namePrefix/namePrefixSource, notes live in x["gocassini.com"],
# `published` is session-local, and every other member goes to passthrough.
MEETING_KEYS = (
    "version",
    "title",
    "titleSource",
    "speakers",
    "speakerMerges",
    "transcript",
    "etaMinutes",
    "error",
    "quoteOverride",
    "received",
    "recordingDetails",
)
TOP_KEYS = ("id", "revision", "notes", "namePrefix", "namePrefixSource", "published")
EXTRA = {"$ref", "propertyNames", "dependentRequired", "minItems", "examples"}


def _pointer(reference):
    document, _, pointer = reference.partition("#")
    if document != "../meeting.schema.json":
        raise ValueError("unsupported-schema-ref")
    value = MEETING_SCHEMA
    for part in filter(None, pointer.split("/")):
        value = value[int(part)] if isinstance(value, list) else value[part.replace("~1", "/").replace("~0", "~")]
    return value


def valid(value, schema=SCHEMA):
    if set(schema) - library_schema.KEYWORDS - EXTRA:
        raise ValueError("unsupported-schema-keyword")
    if "$ref" in schema and not library_schema.valid(value, _pointer(schema["$ref"])):
        return False
    if isinstance(value, dict):
        names = schema.get("propertyNames")
        if names is not None and not all(valid(key, names) for key in value):
            return False
        for key, needed in schema.get("dependentRequired", {}).items():
            if key in value and not set(needed) <= value.keys():
                return False
    if isinstance(value, list) and len(value) < schema.get("minItems", 0):
        return False
    plain = {k: v for k, v in schema.items() if k not in EXTRA}
    nested = ("properties", "items", "allOf", "oneOf", "not", "if", "then", "else")
    if not any(k in plain for k in nested):
        return library_schema.valid(value, plain)
    # library_schema recurses with its own vocabulary only, so recurse here for x-schema children.
    shallow = {k: v for k, v in plain.items() if k not in nested}
    if not library_schema.valid(value, shallow):
        return False
    if isinstance(value, dict):
        for key, child in plain.get("properties", {}).items():
            if key in value and not valid(value[key], child):
                return False
    if isinstance(value, list) and "items" in plain and not all(valid(item, plain["items"]) for item in value):
        return False
    if not all(valid(value, child) for child in plain.get("allOf", ())):
        return False
    if "oneOf" in plain and sum(valid(value, child) for child in plain["oneOf"]) != 1:
        return False
    if "not" in plain and valid(value, plain["not"]):
        return False
    if "if" in plain:
        branch = "then" if valid(value, plain["if"]) else "else"
        if branch in plain and not valid(value, plain[branch]):
            return False
    return True


def private_data(meeting, *, origin, automatic_title, tracks=None, base=None):
    """x["codemyriad.io"] for one meeting.json value (MAPPING.md writer split).

    `base` is the existing object, whose members this writer does not know are
    carried forward unchanged (format: Extensibility).
    """
    x = deepcopy(base) if isinstance(base, dict) else {}
    for key in ("namePrefixSource", "passthrough", "tracks"):
        x.pop(key, None)
    x.update(
        schema=1,
        sessionId=meeting["id"],
        origin=origin,
        namePrefix=meeting["namePrefix"],
        revision=meeting["revision"],
        meeting={key: deepcopy(meeting[key]) for key in MEETING_KEYS if key in meeting},
        automaticTitle=automatic_title,
    )
    if "namePrefixSource" in meeting:
        x["namePrefixSource"] = meeting["namePrefixSource"]
    passthrough = {k: deepcopy(v) for k, v in meeting.items() if k not in MEETING_KEYS and k not in TOP_KEYS}
    if passthrough:
        x["passthrough"] = passthrough
    if tracks is not None:
        x["tracks"] = deepcopy(tracks)
    elif isinstance(base, dict) and "tracks" in base:
        x["tracks"] = deepcopy(base["tracks"])
    return x


def notes_domain(base, notes):
    """x["gocassini.com"] with `notes` (absent when empty); its other members carried forward."""
    domain = deepcopy(base) if isinstance(base, dict) else {}
    if notes:
        domain["notes"] = deepcopy(list(notes))
    else:
        domain.pop("notes", None)
    return domain


def rebuild(x, notes_owner=None, *, session_exists=True):
    """meeting.json from a file's x (MAPPING.md "Rebuilding meeting.json from a file")."""
    notes = (notes_owner or {}).get("notes") if isinstance(notes_owner, dict) else None
    value = dict(id=x["sessionId"], revision=x["revision"], namePrefix=x["namePrefix"], notes=deepcopy(notes or []))
    value.update(deepcopy(x["meeting"]))
    value.update(deepcopy(x.get("passthrough", {})))
    if "namePrefixSource" in x:
        value["namePrefixSource"] = x["namePrefixSource"]
    if session_exists:
        value["published"] = True
    return value
