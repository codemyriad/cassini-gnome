# SPDX-License-Identifier: GPL-3.0-or-later
"""Every package format must build the writing-down bundle from the same pins."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "whistle/pins.json").read_text())
ARTIFACTS = PINS["artifacts"]
FLATPAK = [
    ROOT / "build-aux/flatpak" / name for name in ("com.gocassini.Viewer.json", "com.gocassini.Viewer.Devel.json")
]


@pytest.mark.parametrize(
    "path",
    [ROOT / "build-aux/rpm/cassini-viewer.spec", ROOT / "build-aux/arch/PKGBUILD", *FLATPAK],
    ids=lambda p: p.name,
)
def test_every_pinned_url_and_sha256_is_in_the_package_sources(path):
    text = path.read_text()
    for name, info in ARTIFACTS.items():
        assert info["url"] in text, f"{path.name} does not fetch {name} from its pinned URL"
        if path.suffix != ".spec":  # rpmbuild has no checksum field; fetch.py --offline checks
            assert info["sha256"] in text, f"{path.name} does not pin the sha256 of {name}"


def test_rpm_sources_are_copied_under_the_names_fetch_expects():
    spec = (ROOT / "build-aux/rpm/cassini-viewer.spec").read_text()
    sources = dict(re.findall(r"^Source(\d+):\s*(\S+)$", spec, re.M))
    copies = dict(re.findall(r"^cp -p %\{SOURCE(\d+)\} \.whistle-cache/(\S+)$", spec, re.M))
    assert {name: sources[number] for number, name in copies.items()} == {
        name: info["url"] for name, info in ARTIFACTS.items()
    }


def test_pkgbuild_checksums_follow_the_source_order():
    pkgbuild = (ROOT / "build-aux/arch/PKGBUILD").read_text()
    names = re.findall(r"'([\w.]+)::https://", pkgbuild)
    sums = re.findall(r"'([0-9a-f]{64})'", pkgbuild)
    assert set(names) == set(ARTIFACTS)
    assert sums == [ARTIFACTS[name]["sha256"] for name in names]


@pytest.mark.parametrize("path", FLATPAK, ids=lambda p: p.name)
def test_flatpak_bundles_whistle_and_matching_wheels_only_on_x86_64(path):
    manifest = json.loads(path.read_text())
    runtime, app = manifest["modules"]
    assert runtime["only-arches"] == ["x86_64"] and runtime["build-options"]["no-debuginfo"]
    # The wheels' cpXY tag must match the runtime's Python: GNOME 50 has 3.13.
    assert manifest["runtime-version"] == "50"
    wheels = [s["url"].rsplit("/", 1)[1] for s in runtime["sources"]]
    assert len(wheels) == 2 and all("-cp313-cp313-" in w and "x86_64" in w for w in wheels)
    assert all(re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) for s in runtime["sources"])
    cached = {s["dest-filename"]: s for s in app["sources"] if s.get("dest") == ".whistle-cache"}
    assert {name: (s["url"], s["sha256"]) for name, s in cached.items()} == {
        name: (info["url"], info["sha256"]) for name, info in ARTIFACTS.items()
    }
    assert all(s["only-arches"] == ["x86_64"] for s in cached.values())
    options = app["build-options"]
    assert options["no-debuginfo"]
    assert options["arch"]["x86_64"]["config-opts"] == ["-Dwhistle=enabled", "-Dwhistle_cache=.whistle-cache"]
    assert "-Dwhistle=enabled" not in app.get("config-opts", [])


def test_debian_package_is_amd64_and_keeps_the_bundle_unstripped():
    control = (ROOT / "debian/control").read_text()
    rules = (ROOT / "debian/rules").read_text()
    assert "Architecture: amd64" in control and "Architecture: all" not in control
    for dependency in ("python3-numpy", "python3-onnxruntime"):
        assert re.search(rf"^\s+{dependency},$", control, re.M)
    assert "-Dwhistle=enabled" in rules
    for helper in ("dh_strip", "dh_dwz", "dh_strip_nondeterminism"):
        assert f"\t{helper} -Xwhistle/\n" in rules
