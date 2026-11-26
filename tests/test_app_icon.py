"""D-1176: the GNOME app icon is cassini.talk's Saturn on the dark tile.

The scalable icon embeds the vendored ``saturn-nav.svg`` unchanged (not
``saturn-mid``/``saturn-full``), and the symbolic icon stays a single
``#2e3436`` fill so GTK can recolour it. gi-free on purpose.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "data" / "icons" / "hicolor"
APP_ICON = ICONS / "scalable" / "apps" / "io.codemyriad.CassiniTalk.svg"
SYMBOLIC = ICONS / "symbolic" / "apps" / "io.codemyriad.CassiniTalk-symbolic.svg"
SATURN_NAV = ROOT / "ux" / "brand" / "mark" / "saturn-nav.svg"
SVG = "{http://www.w3.org/2000/svg}"


def _nav_body() -> str:
    text = SATURN_NAV.read_text(encoding="utf-8")
    return text[text.index("<defs>") : text.rindex("</svg>")]


def test_app_icon_embeds_saturn_nav_unchanged():
    icon = APP_ICON.read_text(encoding="utf-8")
    assert _nav_body() in icon


def test_app_icon_tile_on_the_gnome_grid():
    root = ET.parse(APP_ICON).getroot()
    assert root.get("viewBox") == "0 0 128 128"
    rects = [r.attrib for r in root.findall(f"{SVG}rect")]
    face = {"x": "12", "y": "12", "width": "104", "height": "96", "rx": "20"}
    assert any(all(r.get(k) == v for k, v in face.items()) and r.get("fill") == "#0D1015" for r in rects)
    assert any(r.get("y") == "20" and r.get("fill") == "#030405" for r in rects)
    nested = root.find(f"{SVG}svg")
    assert nested is not None and nested.get("viewBox") == "0 0 184 120"
    # The drawn outline (x 0.1..177.2 of 184) spans 74% of the 104 px face.
    scale = float(nested.get("width")) / 184
    assert abs((177.2 - 0.1) * scale / 104 - 0.74) < 0.005


def test_symbolic_icon_is_one_recolourable_fill():
    text = SYMBOLIC.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.get("viewBox") == "0 0 16 16"
    assert set(re.findall(r'fill="([^"]+)"', text)) == {"#2e3436"}
    assert "url(#" not in text and "filter" not in text
