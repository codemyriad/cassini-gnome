# SPDX-License-Identifier: GPL-3.0-or-later
"""Release input provenance and CI accounting without publishing or GitHub writes."""

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


def module(name):
    path = Path(__file__).parents[1] / "build-aux/ci" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def runs():
    return [
        dict(
            id=i,
            path=".github/workflows/" + name,
            head_sha="a" * 40,
            event="push",
            head_branch="main",
            status="completed",
            conclusion="success",
            html_url="https://example.invalid/run/" + str(i),
        )
        for i, name in enumerate(module("green_main").WORKFLOWS, 1)
    ]


def test_release_requires_the_exact_commit_and_every_latest_main_workflow():
    policy = module("green_main")
    good = runs()
    assert set(policy.select_runs(good, "a" * 40)) == set(policy.WORKFLOWS)
    for altered in (
        dict(head_sha="b" * 40),
        dict(event="pull_request"),
        dict(head_branch="feature"),
        dict(status="in_progress", conclusion=None),
        dict(conclusion="failure"),
    ):
        bad = [dict(run, **altered) if run["id"] == 1 else run for run in good]
        with pytest.raises(ValueError):
            policy.select_runs(bad, "a" * 40)
    # A later failed run cannot borrow an earlier green run's artifacts.
    with pytest.raises(ValueError, match="not green"):
        policy.select_runs(good + [dict(good[0], id=100, conclusion="failure")], "a" * 40)


def test_release_requires_every_job_green_and_the_build_and_meson_test_jobs_present():
    policy = module("green_main")
    good = [
        dict(name="Build / Meson build profiles", conclusion="success"),
        dict(name="Meson tests / Meson tests (minimal environment)", conclusion="success"),
        dict(name="Desktop / GNOME 50 / installed deb and Flatpak", conclusion="success"),
    ]
    policy.validate_jobs(good, "u")
    for bad in (
        [],
        good[1:],
        [good[0], good[2]],
        [dict(job, conclusion="skipped") if job is good[1] else job for job in good],
        good + [dict(name="Offline / pinned-offline-recognition", conclusion="failure")],
    ):
        with pytest.raises(ValueError):
            policy.validate_jobs(bad, "u")


def test_release_requires_all_unexpired_package_artifacts():
    policy = module("green_main")
    good = [dict(name=name, expired=False) for name in policy.PACKAGES | policy.BUILD_PRODUCTS]
    policy.validate_artifacts(good)
    for changed in (good[:-1], good + [good[0]], [dict(a, expired=True) for a in good]):
        with pytest.raises(ValueError):
            policy.validate_artifacts(changed)


def test_ci_minutes_round_each_job_and_unstarted_jobs_are_not_falsely_complete():
    result = module("measure_ci").summarize(
        [
            dict(executionSeconds=61, queueSeconds=2),
            dict(executionSeconds=1, queueSeconds=8),
            dict(executionSeconds=None, queueSeconds=None),
        ]
    )
    assert result["jobCount"] == 3 and result["completedJobs"] == 2
    assert result["estimatedRoundedLinuxMinutes"] == 3
    assert result["executionMinutes"] == 62 / 60 and result["medianQueueSeconds"] == 5


def test_usage_api_minutes_are_separate_from_rounded_resource_estimates():
    usage = module("measure_ci").reported_minutes
    assert usage([{"UBUNTU": {"total_ms": 0}}]) == 0
    assert usage([{"UBUNTU": {"total_ms": 90000}}, {"UBUNTU": {"total_ms": 30000}}]) == 2
    assert usage([None]) is None and usage([]) is None


@pytest.fixture
def candidate(tmp_path):
    files = {
        "meson.build": "project('cassini-talk',\n version: '7.8.9',\n)\n",
        "debian/changelog": "cassini-talk (7.8.9+talk1) UNRELEASED; urgency=medium\n",
        "build-aux/rpm/cassini-talk.spec": "Version: 7.8.9\n",
        "build-aux/arch/PKGBUILD": "pkgver=7.8.9\n",
        "NEWS": "Version 7.8.9\n~~~~~~~~~~~~~\nNot released yet.\n",
        "browser-extension/manifest.json": '{"version":"0.2.13"}\n',
        "data/cassini-talk.1": '.TH CASSINI-TALK 1 "October 2026" "cassini-talk 7.8.9" "User Commands"\n',
        "data/io.codemyriad.CassiniTalk.metainfo.xml.in": (
            '<component><releases><release version="7.8.9" date="2026-10-29"/></releases></component>'
        ),
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_release_metadata_accepts_a_consistent_candidate_without_creating_a_tag(candidate):
    result = module("release_metadata").check(candidate, "v7.8.9")
    assert result["version"] == "7.8.9" and result["date"] == "2026-10-29"
    assert result["extensionVersion"] == "0.2.13"
    assert len(result["versions"]) == 7
    assert not (candidate / ".git").exists()
    with pytest.raises(ValueError, match="does not match"):
        module("release_metadata").check(candidate, "v7.8.10")


@pytest.mark.parametrize(
    "name",
    [
        "meson.build",
        "debian/changelog",
        "build-aux/rpm/cassini-talk.spec",
        "build-aux/arch/PKGBUILD",
        "NEWS",
        "data/cassini-talk.1",
        "data/io.codemyriad.CassiniTalk.metainfo.xml.in",
    ],
)
def test_release_metadata_rejects_any_stale_package_or_notes_version(candidate, name):
    path = candidate / name
    path.write_text(path.read_text().replace("7.8.9", "7.8.8"))
    with pytest.raises(ValueError, match="versions disagree"):
        module("release_metadata").check(candidate)


@pytest.mark.parametrize("replacement", [' date="2026-02-30"', ""])
def test_release_metadata_requires_a_real_metainfo_date(candidate, replacement):
    path = candidate / "data/io.codemyriad.CassiniTalk.metainfo.xml.in"
    path.write_text(path.read_text().replace(' date="2026-10-29"', replacement))
    with pytest.raises(ValueError):
        module("release_metadata").check(candidate)


def test_release_metadata_rejects_the_old_browser_companion(candidate):
    (candidate / "browser-extension/manifest.json").write_text('{"version":"0.2.1"}\n')
    with pytest.raises(ValueError, match="Browser companion version must be 0.2.13"):
        module("release_metadata").check(candidate)


def test_installed_metadata_validation_checks_the_packaged_files_and_both_validators(tmp_path):
    prefix = tmp_path / "usr"
    launcher = prefix / "bin/cassini-talk"
    metainfo = prefix / "share/metainfo/io.codemyriad.CassiniTalk.metainfo.xml"
    desktop = prefix / "share/applications/io.codemyriad.CassiniTalk.desktop"
    extension = prefix / "share/cassini-talk/browser-extension/manifest.json"
    for path in (launcher, metainfo, desktop, extension):
        path.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('VERSION = "7.8.9"\n')
    metainfo.write_text(
        '<component><id>io.codemyriad.CassiniTalk</id><releases><release version="7.8.9"/></releases></component>'
    )
    desktop.write_text("[Desktop Entry]\n")
    extension.write_text('{"version":"0.2.13"}\n')
    calls = []

    def run(command, *, check):
        assert check is True
        calls.append(command)

    policy = module("validate_installed")
    result = policy.validate(prefix, "io.codemyriad.CassiniTalk", "7.8.9", run=run)
    assert result["version"] == "7.8.9" and result["appstream"] == result["desktop"] == "passed"
    assert result["extensionVersion"] == "0.2.13"
    assert calls == [
        ["appstreamcli", "validate", "--no-net", str(metainfo)],
        ["desktop-file-validate", str(desktop)],
    ]
    for path in (launcher, metainfo):
        original = path.read_text()
        path.write_text(original.replace("7.8.9", "7.8.8"))
        with pytest.raises(ValueError, match="version"):
            policy.validate(prefix, "io.codemyriad.CassiniTalk", "7.8.9", run=run)
        path.write_text(original)
    calls.clear()
    extension.write_text('{"version":"0.2.1"}\n')
    with pytest.raises(ValueError, match="Installed browser companion version must be 0.2.13"):
        policy.validate(prefix, "io.codemyriad.CassiniTalk", "7.8.9", run=run)
    assert calls == []


def test_release_notes_are_the_tagged_versions_news_section():
    notes = module("release_notes").notes
    news = "Version 2.0.0\n~~~~~~~~~~~~~\nReleased: 2026-11-05\n\n- New\n  line\n\nVersion 1.0.0\n~~~~~~~~~~~~~\nReleased: 2026-10-01\n\n- Old\n"
    assert notes(news, "2.0.0") == "- New\n  line\n"
    assert notes(news, "1.0.0") == "- Old\n"
    with pytest.raises(ValueError, match="no notes for version 3.0.0"):
        notes(news, "3.0.0")


def test_the_tag_workflow_publishes_the_news_section_as_release_notes():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text()
    assert 'build-aux/ci/release_notes.py "$GITHUB_REF_NAME" > notes.md' in workflow
    assert "--notes-file notes.md --generate-notes" in workflow
    version = module("release_metadata").check(root)["version"]
    assert module("release_notes").notes((root / "NEWS").read_text(), version).strip()


def test_release_sums_cover_exactly_the_five_packages_by_name(tmp_path):
    assets = module("release_sums")
    contents = {
        "deb/cassini-talk_0.4.0~ubuntu26.04_amd64.deb": b"ubuntu",
        "deb/cassini-talk_0.4.0~debian-testing_amd64.deb": b"debian",
        "rpm/cassini-talk-0.4.0-1.fc44.x86_64.rpm": b"fedora",
        "rpm/cassini-talk-0.4.0-1.1.x86_64.rpm": b"opensuse",
        "arch/cassini-talk-0.4.0-1-x86_64.pkg.tar.zst": b"arch",
        "other/notes.txt": b"not a package",
    }
    for name, data in contents.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    files = assets.packages(tmp_path)
    sums = assets.write_sums(files, tmp_path / "SHA256SUMS")
    lines = sums.read_text().splitlines()
    expected = {
        f"{hashlib.sha256(data).hexdigest()}  {Path(name).name}"
        for name, data in contents.items()
        if not name.startswith("other/")
    }
    assert len(lines) == 5 and set(lines) == expected
    if shutil.which("sha256sum"):
        flat = tmp_path / "published"
        flat.mkdir()
        for path in [*files, sums]:
            shutil.copy(path, flat / path.name)
        subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=flat, check=True, capture_output=True)
        (flat / files[0].name).write_bytes(b"tampered")
        assert subprocess.run(
            ["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=flat, capture_output=True
        ).returncode


@pytest.mark.parametrize("drop", [0, 2, 4])
def test_release_sums_refuse_a_missing_or_extra_package(tmp_path, drop):
    assets = module("release_sums")
    names = [
        "a~ubuntu26.04_amd64.deb",
        "a~debian-testing_amd64.deb",
        "a.fc44.x86_64.rpm",
        "a.1.x86_64.rpm",
        "a-x86_64.pkg.tar.zst",
    ]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / names[drop]).unlink()
    with pytest.raises(ValueError, match="Expected"):
        assets.packages(tmp_path)
    (tmp_path / names[drop]).write_bytes(b"back")
    (tmp_path / ("extra-" + names[drop])).write_bytes(b"extra")
    with pytest.raises(ValueError, match="Expected"):
        assets.packages(tmp_path)


def test_the_tag_workflow_attaches_sha256sums_and_verifies_the_published_files():
    workflow = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    assert "release_sums.py artifacts --sums SHA256SUMS" in workflow
    assert '--generate-notes "${assets[@]}"' in workflow
    assert 'gh release download "$GITHUB_REF_NAME"' in workflow
    assert "sha256sum --check --strict SHA256SUMS" in workflow
    assert 'test "$(wc -l < SHA256SUMS)" -eq 5' in workflow
