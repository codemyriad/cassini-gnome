# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate the vendored meeting schema's vocabulary without a runtime dependency.

This is deliberately limited to the checked-in schema, not a general JSON Schema
implementation. Unknown validation keywords fail closed. JSON is parsed strictly
by the caller; unsupported document versions are handled before validation.
"""

import json
import math
import re
from pathlib import Path

SCHEMA = json.loads(Path(__file__).with_name("meeting.schema.json").read_text())
ANNOTATIONS = {"$schema", "$comment", "title", "description", "additionalProperties"}
KEYWORDS = ANNOTATIONS | {
    "type",
    "const",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "properties",
    "required",
    "items",
    "maxItems",
    "allOf",
    "oneOf",
    "if",
    "then",
    "else",
    "not",
}
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def valid(value, schema=SCHEMA):
    if set(schema) - KEYWORDS:
        raise ValueError("unsupported-schema-keyword")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "null": value is None,
        "integer": type(value) is int,
        "number": type(value) is int or type(value) is float and math.isfinite(value),
        "boolean": type(value) is bool,
    }
    if types and not any(checks[kind] for kind in types):
        return False
    if "const" in schema and (type(value) is bool or value != schema["const"]):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if type(value) in (int, float) and (
        "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]
    ):
        return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            return False
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return False
        if schema.get("format") == "uuid" and not UUID_PATTERN.fullmatch(value):
            return False
    if isinstance(value, dict):
        if not set(schema.get("required", ())) <= value.keys():
            return False
        if any(key in value and not valid(value[key], child) for key, child in schema.get("properties", {}).items()):
            return False
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", math.inf):
            return False
        if "items" in schema and not all(valid(item, schema["items"]) for item in value):
            return False
    if not all(valid(value, child) for child in schema.get("allOf", ())):
        return False
    if "oneOf" in schema and sum(valid(value, child) for child in schema["oneOf"]) != 1:
        return False
    if "not" in schema and valid(value, schema["not"]):
        return False
    if "if" in schema:
        branch = "then" if valid(value, schema["if"]) else "else"
        if branch in schema and not valid(value, schema[branch]):
            return False
    return True
