#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare the minimal Ubuntu 26.04 source-validation image for build.yml and meson-tests.yml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_CI_DIR = str(Path(__file__).resolve().parent)
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)
import ci_image_core  # noqa: E402


def main(argv: list[str] | None = None, *, root: Path = ci_image_core.ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="@sha256-pinned ubuntu:26.04 base image reference")
    args = parser.parse_args(argv)
    print(ci_image_core.prepare_spec_image(ci_image_core.UBUNTU_BUILD_SPEC, args.base, root=root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
