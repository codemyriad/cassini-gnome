#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate installed package metadata and launcher version without opening GTK."""

import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def validate(prefix, app_id, version, *, run=subprocess.run):
    extension = prefix / "share/cassini-viewer/browser-extension/manifest.json"
    extension_version = json.loads(extension.read_text()).get("version")
    if extension_version != "0.2.3":
        raise ValueError("Installed browser companion version must be 0.2.3")
    launcher = (prefix / "bin/cassini-talk").read_text()
    match = re.search(r'^VERSION = "([^"]+)"$', launcher, re.MULTILINE)
    if match is None or match[1] != version:
        raise ValueError("Installed launcher version does not match the source")
    metainfo = prefix / f"share/metainfo/{app_id}.metainfo.xml"
    desktop = prefix / f"share/applications/{app_id}.desktop"
    component = ET.parse(metainfo).getroot()
    release = component.find("releases/release")
    if component.findtext("id") != app_id or release is None or release.get("version") != version:
        raise ValueError("Installed metainfo ID/version does not match the source")
    run(["appstreamcli", "validate", "--no-net", str(metainfo)], check=True)
    run(["desktop-file-validate", str(desktop)], check=True)
    return dict(
        version=version,
        appId=app_id,
        prefix=str(prefix),
        appstream="passed",
        desktop="passed",
        extensionVersion=extension_version,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--prefix", type=Path, default=Path("/usr"))
    parser.add_argument("--app-id", default="io.codemyriad.CassiniTalk")
    args = parser.parse_args()
    print(json.dumps(validate(args.prefix, args.app_id, args.version), indent=2))


if __name__ == "__main__":
    main()
