#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check release versions without tagging, publishing or changing any files."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path


def value(root, path, pattern):
    match = re.search(pattern, (root / path).read_text(), re.MULTILINE)
    if match is None:
        raise ValueError(f"No version found in {path}")
    return match[1]


def check(root, tag=None):
    extension_version = json.loads((root / "browser-extension/manifest.json").read_text()).get("version")
    if extension_version != "0.2.6":
        raise ValueError("Browser companion version must be 0.2.6")
    versions = {
        "meson": value(root, "meson.build", r"^\s*version:\s*'([^']+)'"),
        "debian": value(root, "debian/changelog", r"\Acassini-talk \(([^)]+)\)").split("+")[0].split("-")[0],
        "rpm": value(root, "build-aux/rpm/cassini-talk.spec", r"^Version:\s*(\S+)"),
        "arch": value(root, "build-aux/arch/PKGBUILD", r"^pkgver=(\S+)"),
        "news": value(root, "NEWS", r"^Version (\d+\.\d+\.\d+)$"),
        "man": value(root, "data/cassini-talk.1", r'\A\.TH [^\n]*"cassini-talk ([^"]+)"'),
    }
    release = ET.parse(root / "data/io.codemyriad.CassiniTalk.metainfo.xml.in").find("releases/release")
    if release is None:
        raise ValueError("Metainfo has no release entry")
    versions["metainfo"] = release.get("version")
    version = versions["meson"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or any(v != version for v in versions.values()):
        raise ValueError(f"Release versions disagree: {versions}")
    if tag is not None and tag != "v" + version:
        raise ValueError(f"Tag {tag} does not match version {version}")
    release_date = release.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ValueError("Metainfo needs a valid release date")
    date.fromisoformat(release_date)
    return dict(version=version, date=release_date, versions=versions, tag=tag, extensionVersion=extension_version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--tag", help="check a proposed tag name; never creates it")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.root, args.tag)
    except (OSError, ValueError, ET.ParseError) as error:
        parser.error(str(error))
    report = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
