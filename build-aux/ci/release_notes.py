#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print one version's NEWS section as Markdown release notes."""

import argparse
import re
from pathlib import Path


def notes(news, version):
    """The NEWS section for `version`, without its heading, underline and date line."""
    sections = re.split(r"^Version (\d+\.\d+\.\d+)\n~+\n", news, flags=re.MULTILINE)
    for name, body in zip(sections[1::2], sections[2::2], strict=True):
        if name == version:
            body = re.sub(r"\AReleased: [^\n]*\n", "", body).strip()
            if body:
                return body + "\n"
    raise ValueError(f"NEWS has no notes for version {version}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="X.Y.Z, or the tag vX.Y.Z")
    parser.add_argument("--news", type=Path, default=Path(__file__).resolve().parents[2] / "NEWS")
    args = parser.parse_args()
    try:
        print(notes(args.news.read_text(), args.version.removeprefix("v")), end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
