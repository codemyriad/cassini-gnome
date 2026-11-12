# SPDX-License-Identifier: GPL-3.0-or-later
"""Release input provenance and CI accounting without publishing or GitHub writes."""

import hashlib
import importlib.util
import os
import re
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
    # When both a fast push run and a full-matrix workflow_dispatch run are green on main,
    # select_runs prefers the latest full-matrix run so all pinned native package artifacts are present.
    dispatched = dict(good[0], id=50, event="workflow_dispatch")
    later_push = dict(good[0], id=60, event="push")
    assert policy.select_runs([dispatched, later_push], "a" * 40)["ci.yml"]["id"] == 50
    # Scheduled nightly runs probe unpinned rolling base images outside merge and release gates:
    # they neither block nor supply release artifacts.
    red_schedule = dict(good[0], id=70, event="schedule", conclusion="failure")
    green_schedule = dict(good[0], id=80, event="schedule", conclusion="success")
    assert policy.select_runs([later_push, red_schedule, green_schedule], "a" * 40)["ci.yml"]["id"] == 60
    with pytest.raises(ValueError, match="No main run"):
        policy.select_runs([green_schedule], "a" * 40)


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
        "browser-extension/manifest.json": '{"version":"0.2.26"}\n',
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
    assert result["extensionVersion"] == "0.2.26"
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
    (candidate / "browser-extension/manifest.json").write_text('{"version":"0.2.25"}\n')
    with pytest.raises(ValueError, match="Browser companion version must be 0.2.26"):
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
    extension.write_text('{"version":"0.2.26"}\n')
    calls = []

    def run(command, *, check):
        assert check is True
        calls.append(command)

    policy = module("validate_installed")
    result = policy.validate(prefix, "io.codemyriad.CassiniTalk", "7.8.9", run=run)
    assert result["version"] == "7.8.9" and result["appstream"] == result["desktop"] == "passed"
    assert result["extensionVersion"] == "0.2.26"
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
    extension.write_text('{"version":"0.2.25"}\n')
    with pytest.raises(ValueError, match="Installed browser companion version must be 0.2.26"):
        policy.validate(prefix, "io.codemyriad.CassiniTalk", "7.8.9", run=run)
    assert calls == []


def test_release_notes_are_the_tagged_versions_news_section():
    notes = module("release_notes").notes
    news = "Version 2.0.0\n~~~~~~~~~~~~~\nReleased: 2026-11-05\n\n- New\n  line\n\nVersion 1.0.0\n~~~~~~~~~~~~~\nReleased: 2026-10-01\n\n- Old\n"
    assert notes(news, "2.0.0") == "- New\n  line\n"
    assert notes(news, "1.0.0") == "- Old\n"
    with pytest.raises(ValueError, match="no notes for version 3.0.0"):
        notes(news, "3.0.0")


def news_version(root):
    meson = re.search(r"^\s*version:\s*'([^']+)'", (root / "meson.build").read_text(), re.M)[1]
    if match := re.fullmatch(r"(\d+\.\d+\.\d+)~beta\.([1-9]\d*)", meson):
        return module("release_metadata").check_prerelease(root, f"{match[1]}-beta.{match[2]}")["newsVersion"]
    return module("release_metadata").check(root)["version"]


def test_the_tag_workflow_publishes_the_news_section_as_release_notes():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text()
    assert 'python3 build-aux/ci/release_notes.py "$GITHUB_REF_NAME" > /dev/null' in workflow
    assert '"repos/$GITHUB_REPOSITORY/releases/generate-notes"' in workflow
    assert 'build-aux/ci/release_notes.py "$GITHUB_REF_NAME" --changelog changelog.md > notes.md' in workflow
    assert "--generate-notes" not in workflow
    assert '--notes-file notes.md "${assets[@]}"' in workflow
    version = news_version(root)
    assert module("release_notes").notes((root / "NEWS").read_text(), version).strip()


def test_stable_release_notes_refuse_missing_placeholder_or_developer_text():
    rn = module("release_notes")
    for bad_body in (
        "Not released yet.",
        "- Fix crash from #392.",
        "- Enforce D95 parity.",
        "- Keep CI green on rolling distros.",
        "- Please log out and back in.",
        "- Update Cassini's panel menu.",
    ):
        news = f"Version 1.2.3\n~~~~~~~~~~~~~\nReleased: 2026-11-12\n\n{bad_body}\n"
        with pytest.raises(ValueError):
            rn.notes(news, "1.2.3")


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
    assert "release_sums.py artifacts --stage staged --sums SHA256SUMS > assets.txt" in workflow
    assert '--notes-file notes.md "${assets[@]}"' in workflow
    assert "--generate-notes" not in workflow
    assert 'gh release download "$GITHUB_REF_NAME"' in workflow
    assert "sha256sum --check --strict SHA256SUMS" in workflow
    assert 'test "$(wc -l < SHA256SUMS)" -eq 5' in workflow


def test_release_sums_name_each_package_as_github_publishes_it(tmp_path):
    # v0.4.0: GitHub published ...~ubuntu26.04... as ....ubuntu26.04..., so the
    # SHA256SUMS lines for both .debs named files the release did not have.
    sums = module("release_sums")
    assert sums.asset_name("cassini-talk_0.4.0~ubuntu26.04_amd64.deb") == "cassini-talk_0.4.0.ubuntu26.04_amd64.deb"
    assert sums.asset_name("cassini-talk-0.4.0-1.fc44.x86_64.rpm") == "cassini-talk-0.4.0-1.fc44.x86_64.rpm"
    names = {
        "cassini-talk_0.4.0~ubuntu26.04_amd64.deb": b"u",
        "cassini-talk_0.4.0~debian-testing_amd64.deb": b"d",
        "cassini-talk-0.4.0-1.fc44.x86_64.rpm": b"f",
        "cassini-talk-0.4.0-1.opensuse_tumbleweed.x86_64.rpm": b"o",
        "cassini-talk-0.4.0-1-x86_64.pkg.tar.zst": b"a",
    }
    for name, data in names.items():
        (tmp_path / name).write_bytes(data)
    staged = sums.stage(sums.packages(tmp_path), tmp_path / "staged")
    written = sums.write_sums(staged, tmp_path / "staged" / "SHA256SUMS").read_text().splitlines()
    listed = {line.split("  ", 1)[1] for line in written}
    assert listed == {p.name for p in staged} == {sums.asset_name(n) for n in names}
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in listed)
    if shutil.which("sha256sum"):
        subprocess.run(
            ["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=tmp_path / "staged", check=True, capture_output=True
        )


def test_release_sums_refuse_two_packages_with_one_published_name(tmp_path):
    sums = module("release_sums")
    (tmp_path / "a~b.deb").write_bytes(b"1")
    (tmp_path / "a.b.deb").write_bytes(b"2")
    with pytest.raises(ValueError, match="share a published name"):
        sums.stage(sorted(tmp_path.glob("*.deb")), tmp_path / "staged")


def create_release_script():
    """The release workflow's own "Create release" run block, as bash runs it."""
    workflow = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    step = workflow.split("      - name: Create release\n", 1)[1]
    block = step.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
    return "\n".join(line.removeprefix("          ") for line in block.splitlines()) + "\n"


def run_create_release(
    tmp_path,
    debs,
    *,
    latest_tag="v0.4.1",
    generated_changelog="## What's Changed\n* feat: search meeting titles in #389 by @dev\n",
):
    """Run that block under bash -e (as GitHub runs it) against fake artifacts and a fake gh."""
    import json

    root = Path(__file__).parents[1]
    (tmp_path / "source").symlink_to(root)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in [
        *debs,
        "cassini-talk-1-1.fc44.x86_64.rpm",
        "cassini-talk-1-1.os.x86_64.rpm",
        "cassini-talk-1-1-x86_64.pkg.tar.zst",
    ]:
        (artifacts / name).write_bytes(name.encode())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['GH_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['api'] and any(a.endswith('/releases/latest') for a in args):\n"
        "    tag = os.environ.get('FAKE_LATEST_TAG', '')\n"
        "    if not tag:\n"
        "        sys.exit(1)\n"
        "    print(tag)\n"
        "elif args[:1] == ['api'] and any(a.endswith('/releases/generate-notes') for a in args):\n"
        "    sys.stdout.write(os.environ.get('FAKE_CHANGELOG', ''))\n"
    )
    (bin_dir / "gh").write_text(fake_gh)
    (bin_dir / "gh").chmod(0o755)
    version = news_version(root)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        GH_CALLS=str(tmp_path / "gh-calls"),
        FAKE_LATEST_TAG="" if latest_tag is None else latest_tag,
        FAKE_CHANGELOG=generated_changelog,
        GITHUB_REF_NAME="v" + version,
        GITHUB_REPOSITORY="example/repo",
    )
    script = tmp_path / "create.sh"
    script.write_text(create_release_script())
    result = subprocess.run(["bash", "-e", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = (
        [json.loads(line) for line in (tmp_path / "gh-calls").read_text().splitlines()]
        if (tmp_path / "gh-calls").exists()
        else None
    )
    return result, calls


@pytest.mark.skipif(not shutil.which("bash"), reason="bash runs the workflow step")
def test_a_refused_staging_never_creates_a_release(tmp_path):
    # #229 Codex: staging in a process substitution lost its failure, so two .debs
    # with one published name reached `gh release create` with zero assets.
    result, calls = run_create_release(tmp_path, ["a~b_amd64.deb", "a.b_amd64.deb"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "share a published name" in result.stderr
    assert calls is None, calls


@pytest.mark.skipif(not shutil.which("bash"), reason="bash runs the workflow step")
def test_a_good_staging_creates_the_release_with_six_assets_and_folded_developer_changelog(tmp_path):
    rn = module("release_notes")
    root = Path(__file__).parents[1]
    result, all_calls = run_create_release(tmp_path, ["a~ubuntu_amd64.deb", "a~debian_amd64.deb"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert all_calls is not None and len(all_calls) == 3
    latest_call, gen_call, create_call = all_calls
    assert "repos/example/repo/releases/latest" in latest_call
    assert "repos/example/repo/releases/generate-notes" in gen_call
    assert "previous_tag_name=v0.4.1" in gen_call
    assert create_call[:2] == ["release", "create"]
    assert "--generate-notes" not in create_call
    attached = [c for c in create_call if c.startswith("staged/") or c == "SHA256SUMS"]
    assert len(attached) == 6 and attached[-1] == "SHA256SUMS"

    published_notes = (tmp_path / "notes.md").read_text(encoding="utf-8")
    visible_top, sep, folded_dev = published_notes.partition(
        "\n\n<details>\n<summary>Developer changelog</summary>\n\n"
    )
    assert sep
    expected_curated = rn.notes((root / "NEWS").read_text(encoding="utf-8"), news_version(root)).strip()
    assert visible_top == expected_curated
    rn.validate_user_text(visible_top)
    assert "#389" not in visible_top
    assert "feat: search meeting titles in #389 by @dev" in folded_dev
    assert folded_dev.endswith("\n</details>\n")


@pytest.mark.skipif(not shutil.which("bash"), reason="bash runs the workflow step")
def test_stable_release_handles_first_release_and_refuses_empty_changelog(tmp_path):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    result, all_calls = run_create_release(first_dir, ["a~ubuntu_amd64.deb", "a~debian_amd64.deb"], latest_tag=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all_calls is not None and len(all_calls) == 3
    assert not any(arg.startswith("previous_tag_name=") for arg in all_calls[1])
    assert all_calls[2][:2] == ["release", "create"]

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    bad_result, bad_calls = run_create_release(
        empty_dir, ["a~ubuntu_amd64.deb", "a~debian_amd64.deb"], generated_changelog="   \n"
    )
    assert bad_result.returncode != 0
    assert bad_calls is not None and not any(c[:2] == ["release", "create"] for c in bad_calls)
