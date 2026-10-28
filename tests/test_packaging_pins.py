# SPDX-License-Identifier: GPL-3.0-or-later
"""Every package format must build the writing-down bundle from the same pins."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "whistle/pins.json").read_text())
ARTIFACTS = PINS["artifacts"]
SHERPA = json.loads((ROOT / "src/cassini_viewer/sherpa-pins.json").read_text())["sherpa-onnx-go-linux"]
SHERPA_FILES = {Path(n).name: pin for n, pin in SHERPA["files"].items() if not n.endswith("cxx-api.so")}
SHERPA_SUMS_SHA = "fa59734ebf92a17c5edc6793b74c8f8e0a184df640597d62f1208ceb0d66ca78"

WHEELS = PINS["onnxruntime"]["wheels"]
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
    sources = dict(re.findall(r"^Source([1-4]):\s*(\S+)$", spec, re.M))
    copies = dict(re.findall(r"^cp -p %\{SOURCE(\d+)\} \.whistle-cache/(\S+)$", spec, re.M))
    assert {name: sources[number] for number, name in copies.items()} == {
        name: info["url"] for name, info in ARTIFACTS.items()
    }
    # Source5 is the ONNX Runtime wheel for the build's Python, kept under its
    # own file name (the URL's), which is what fetch.py --onnxruntime reads.
    wheels = re.findall(r'^%if "%\{\?python3_version_nodots\}" == "(\d+)"\nSource5:\s*(\S+)\n%endif$', spec, re.M)
    assert {"cp" + nodots: url for nodots, url in wheels} == {abi: info["url"] for abi, info in WHEELS.items()}
    assert "\ncp -p %{SOURCE5} .whistle-cache/\n" in spec


def test_rpm_requires_its_python_abi_and_not_the_distribution_onnxruntime():
    spec = (ROOT / "build-aux/rpm/cassini-viewer.spec").read_text()
    assert "Requires:       python(abi) = %{python3_version}" in spec
    assert "Requires:       python3dist(numpy)" in spec
    assert not re.search(r"^(Build)?Requires:.*onnxruntime", spec, re.M)
    assert "%{_libdir}/%{name}\n" in spec and "verify.py --onnxruntime %{buildroot}%{_libdir}/%{name}/python" in spec
    assert "%license whistle/LICENSE-ONNXRuntime-MIT.txt" in spec


def _pkgbuild_array(text, name):
    body = re.search(rf"^{name}=\((.*?)\)$", text, re.M | re.S).group(1)
    return re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", re.sub(r"#.*", "", body))


def test_pkgbuild_checksums_follow_the_source_order():
    pkgbuild = (ROOT / "build-aux/arch/PKGBUILD").read_text()
    sources = ["".join(parts) for parts in _pkgbuild_array(pkgbuild, "source")]
    sums = ["".join(parts) for parts in _pkgbuild_array(pkgbuild, "sha256sums")]
    assert len(sources) == len(sums) == len(ARTIFACTS) + 2 + len(SHERPA_FILES) + 1
    pinned = {name: info["sha256"] for name, info in ARTIFACTS.items()}
    pinned.update({info["url"].rsplit("/", 1)[1]: info["sha256"] for info in WHEELS.values()})
    pinned.update({name: pin["sha256"] for name, pin in SHERPA_FILES.items()})
    pinned["CASSINI-SHA256SUMS"] = SHERPA_SUMS_SHA
    for source, digest in zip(sources[1:], sums[1:], strict=True):
        name = source.split("::")[0] if "::" in source else source.rsplit("/", 1)[1]
        assert pinned[name] == digest, name


def test_pkgbuild_onnxruntime_wheel_matches_its_python_bound():
    pkgbuild = (ROOT / "build-aux/arch/PKGBUILD").read_text()
    depends = ["".join(parts) for parts in _pkgbuild_array(pkgbuild, "depends")]
    assert not any("onnxruntime" in d for d in depends) and "python-numpy" in depends
    lower = next(d for d in depends if d.startswith("python>="))[len("python>=") :]
    upper = next(d for d in depends if d.startswith("python<"))[len("python<") :]
    major, minor = lower.split(".")
    assert upper == f"{major}.{int(minor) + 1}"
    abi = f"cp{major}{minor}"
    assert WHEELS[abi]["url"] in pkgbuild and WHEELS[abi]["sha256"] in pkgbuild
    assert [abi] == [a for a, info in WHEELS.items() if info["url"] in pkgbuild]
    wheel = WHEELS[abi]["url"].rsplit("/", 1)[1]
    assert wheel in "".join("".join(parts) for parts in _pkgbuild_array(pkgbuild, "noextract"))


@pytest.mark.parametrize("path", FLATPAK, ids=lambda p: p.name)
def test_flatpak_bundles_whistle_and_matching_wheels_only_on_x86_64(path):
    manifest = json.loads(path.read_text())
    numpy, app = manifest["modules"]
    assert numpy["only-arches"] == ["x86_64"] and numpy["build-options"]["no-debuginfo"]
    # The wheels' cpXY tag must match the runtime's Python: GNOME 50 has 3.13.
    assert manifest["runtime-version"] == "50"
    wheels = [s["url"].rsplit("/", 1)[1] for s in numpy["sources"]]
    assert len(wheels) == 1 and wheels[0].startswith("numpy-") and "-cp313-cp313-" in wheels[0]
    assert all(re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) for s in numpy["sources"])
    # ONNX Runtime goes the way it goes in every package: the pinned wheel in
    # the cache, unpacked by meson into the app's private directory.
    cached = {
        s.get("dest-filename", s["url"].rsplit("/", 1)[1]): s
        for s in app["sources"]
        if s.get("dest") == ".whistle-cache"
    }
    expected = {name: (info["url"], info["sha256"]) for name, info in ARTIFACTS.items()}
    expected[WHEELS["cp313"]["url"].rsplit("/", 1)[1]] = (WHEELS["cp313"]["url"], WHEELS["cp313"]["sha256"])
    assert {name: (s["url"], s["sha256"]) for name, s in cached.items()} == expected
    assert all(s["only-arches"] == ["x86_64"] for s in cached.values())
    options = app["build-options"]
    assert options["no-debuginfo"]
    assert options["arch"]["x86_64"]["config-opts"] == [
        "-Dwhistle=enabled",
        "-Dwhistle_cache=.whistle-cache",
        "-Dsherpa=enabled",
        "-Dsherpa_cache=.sherpa-cache",
    ]
    assert "-Dwhistle=enabled" not in app.get("config-opts", [])


def test_debian_package_is_amd64_and_keeps_the_bundle_unstripped():
    control = (ROOT / "debian/control").read_text()
    rules = (ROOT / "debian/rules").read_text()
    assert "Architecture: amd64" in control and "Architecture: all" not in control
    assert re.search(r"^\s+python3-numpy,$", control, re.M) and "onnxruntime" not in control
    assert re.search(r"^\s+\$\{cassini:Python\},$", control, re.M) and "-Vcassini:Python=" in rules
    assert "-Dwhistle=enabled" in rules
    for helper in ("dh_strip", "dh_dwz", "dh_strip_nondeterminism"):
        assert f"\t{helper} -Xwhistle/ -Xcassini-viewer/python/ -Xcassini-viewer/sherpa/\n" in rules
    assert "verify.py --onnxruntime $(ORT_DIR)" in rules


def test_every_supported_python_has_a_pinned_onnxruntime_wheel():
    assert sorted(WHEELS) == ["cp311", "cp312", "cp313", "cp314"]
    for abi, info in WHEELS.items():
        name = info["url"].rsplit("/", 1)[1]
        assert name == f"onnxruntime-{PINS['onnxruntime']['version']}-{abi}-{abi}-manylinux_2_28_x86_64.whl"
        assert info["url"].startswith("https://files.pythonhosted.org/") and re.fullmatch(
            r"[0-9a-f]{64}", info["sha256"]
        )


@pytest.mark.parametrize("path", FLATPAK, ids=lambda p: p.name)
def test_flatpak_runtime_sources_match_d16_and_exclude_model_weights(path):
    app = json.loads(path.read_text())["modules"][-1]
    cached = {
        s.get("dest-filename", s["url"].rsplit("/", 1)[1]): s
        for s in app["sources"]
        if s.get("dest") == ".sherpa-cache"
    }
    assert set(cached) == set(SHERPA_FILES) | {"CASSINI-SHA256SUMS"}
    for name, source in cached.items():
        expected = SHERPA_SUMS_SHA if name == "CASSINI-SHA256SUMS" else SHERPA_FILES[name]["sha256"]
        assert source["sha256"] == expected
        assert SHERPA["commit"] in source["url"]
        assert source["only-arches"] == ["x86_64"]
    assert not any("dist.gocassini.com" in s.get("url", "") for s in app["sources"])


def test_rpm_runtime_sources_use_the_pinned_fork_without_model_weights():
    spec = (ROOT / "build-aux/rpm/cassini-viewer.spec").read_text()
    sources = dict(re.findall(r"^Source([6-8]):\s*(\S+)$", spec, re.M))
    assert len(sources) == 3
    assert all(SHERPA["commit"] in url for url in sources.values())
    assert {url.rsplit("/", 1)[1] for url in sources.values()} == set(SHERPA_FILES) | {"CASSINI-SHA256SUMS"}
    assert "dist.gocassini.com" not in spec
    assert "sherpa/fetch.py --cache .sherpa-cache --verify" in spec
    assert "%license %{_datadir}/licenses/%{name}/sherpa" in spec
