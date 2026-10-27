#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check actual rendered pixels, so hardcoded backgrounds cannot fool theme checks."""

import sys
from pathlib import Path

from PIL import Image

root = Path(sys.argv[1])
for name, region in [("library", (440, 85, 900, 120)), ("first-run", (330, 310, 790, 370))]:
    image = Image.open(root / (name + ".png")).convert("RGB")
    assert image.size == (1120, 720)
    assert max(image.getpixel((1100, 650))) < 90, f"{name}: background is not dark"
    bright = sum(min(pixel) > 160 for pixel in image.crop(region).getdata())
    assert bright > 100, f"{name}: primary text has insufficient contrast"
print("Dark Library and first-run have a dark background and legible primary text")
