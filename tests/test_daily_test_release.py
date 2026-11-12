# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract and regression tests for unattended GNOME daily test releases."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    path = ROOT / "build-aux/ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGit:
    """Deterministic in-memory git DAG for zero-skip tests across minimal build containers."""

    def __init__(
        self,
        *,
        head: str,
        commits: list[str],
        tags: dict[str, str] | None = None,
        dirty: str = "",
        timestamp: int = 1794470400,
        side_commits: dict[str, str] | None = None,
        fragments_by_commit: dict[str, dict[str, str]] | None = None,
    ):
        self.head = head
        self.commits = commits  # chronological first-parent chain on main
        self.tags = tags or {}
        self.dirty = dirty
        self.timestamp = timestamp
        # Maps off-main commit SHA -> its merge-base SHA on self.commits
        self.side_commits = side_commits or {}
        self.fragments_by_commit = fragments_by_commit or {}
        self._blobs: dict[str, str] = {}
        for mapping in self.fragments_by_commit.values():
            for raw in mapping.values():
                blob = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
                self._blobs[blob] = raw

    def _resolve(self, rev: str) -> str | None:
        clean = rev.removesuffix("^{commit}")
        if clean == "HEAD":
            return self.head
        if clean in self.tags:
            return self.tags[clean]
        if clean in self.commits or clean in self.side_commits:
            return clean
        return None

    def commit_exists(self, rev: str) -> str | None:
        return self._resolve(rev)

    def is_ancestor(self, older: str, newer: str) -> bool:
        if older == newer:
            return self._resolve(older) is not None
        if older in self.commits and newer in self.commits:
            return self.commits.index(older) <= self.commits.index(newer)
        if older in self.commits and newer in self.side_commits:
            fork = self.side_commits[newer]
            return self.commits.index(older) <= self.commits.index(fork)
        return False

    def run(self, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return self.head
        if args == ("status", "--porcelain"):
            return self.dirty
        if args[:3] == ("show", "-s", "--format=%ct"):
            return str(self.timestamp)
        if args == ("tag", "--list", "v*"):
            return "\n".join(sorted(self.tags))
        if args[0] == "merge-base" and len(args) == 3:
            a, b = args[1], args[2]
            if a in self.side_commits and b in self.commits:
                fork = self.side_commits[a]
                return fork if self.commits.index(fork) <= self.commits.index(b) else b
            if b in self.side_commits and a in self.commits:
                fork = self.side_commits[b]
                return fork if self.commits.index(fork) <= self.commits.index(a) else a
            if a in self.commits and b in self.commits:
                return a if self.commits.index(a) <= self.commits.index(b) else b
            raise AssertionError(f"Unknown merge-base pair: {args}")
        if args[:4] == ("log", "--first-parent", "--reverse", "--format=%H"):
            older, newer = args[4].split("..", 1)
            i = self.commits.index(older)
            j = self.commits.index(newer)
            return "\n".join(self.commits[i + 1 : j + 1])
        if args[:5] == ("log", "--first-parent", "-n", "50", "--format=%H"):
            j = self.commits.index(args[5])
            return "\n".join(reversed(self.commits[: j + 1][-50:]))
        if len(args) == 5 and args[:2] == ("ls-tree", "-r") and args[3:] == ("--", "release-notes.d"):
            rev = self._resolve(args[2]) or args[2]
            entries = self.fragments_by_commit.get(rev, {})
            lines: list[str] = []
            for path, raw in sorted(entries.items()):
                blob = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
                self._blobs[blob] = raw
                lines.append(f"100644 blob {blob}\t{path}")
            return "\n".join(lines)
        if len(args) == 3 and args[:2] == ("cat-file", "blob"):
            return self._blobs[args[2]]
        raise AssertionError(f"Unhandled FakeGit args: {args}")


class FakeGitHub:
    def __init__(
        self,
        *,
        repo: str = "codemyriad/cassini-gnome",
        main_sha: str = "c" * 40,
        ci_pages: list[list[dict]] | None = None,
        jobs_by_run: dict[int, list[dict]] | None = None,
        releases: list[dict] | None = None,
        deployments: list[dict] | None = None,
        statuses_by_dep: dict[int, list[dict]] | None = None,
        pulls_by_commit: dict[str, list[dict]] | None = None,
        tags_by_name: dict[str, dict | None] | None = None,
    ):
        self.repo = repo
        self.main_sha = main_sha
        self.ci_pages = ci_pages or [[]]
        self.jobs_by_run = jobs_by_run or {}
        self.releases = releases if releases is not None else []
        self.deployments = deployments if deployments is not None else []
        self.statuses_by_dep = statuses_by_dep or {}
        self.pulls_by_commit = pulls_by_commit or {}
        self.tags_by_name = tags_by_name or {}
        self.posts: list[tuple[str, dict]] = []
        self.requests: list[str] = []

    def request(self, path: str, *, method: str = "GET", data: dict | None = None, allow_404: bool = False):
        self.requests.append(f"{method} {path}")
        if method == "POST":
            assert data is not None
            self.posts.append((path, data))
            if path == "deployments":
                dep_id = 900 + len(self.posts)
                return {"id": dep_id}
            if path.startswith("deployments/") and path.endswith("/statuses"):
                return {"state": data["state"]}
            raise AssertionError(f"Unexpected POST {path}")

        base_path, _, query = path.partition("?")
        params = dict(item.split("=", 1) for item in query.split("&") if "=" in item)
        page = int(params.get("page", "1"))

        if base_path == "commits/main":
            return {"sha": self.main_sha}
        if base_path == "actions/workflows/ci.yml/runs":
            rows = self.ci_pages[page - 1] if page - 1 < len(self.ci_pages) else []
            return {"workflow_runs": rows}
        if match := re.fullmatch(r"actions/runs/(\d+)/jobs", base_path):
            run_id = int(match[1])
            return {"jobs": self.jobs_by_run[run_id] if page == 1 else []}
        if base_path == "releases":
            return self.releases if page == 1 else []
        if match := re.fullmatch(r"releases/tags/(.+)", base_path):
            tag = match[1]
            if tag in self.tags_by_name and self.tags_by_name[tag] is not None:
                return self.tags_by_name[tag]
            if allow_404:
                return None
            raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)")
        if base_path == "deployments":
            return self.deployments if page == 1 else []
        if match := re.fullmatch(r"deployments/(\d+)/statuses", base_path):
            return self.statuses_by_dep.get(int(match[1]), []) if page == 1 else []
        if match := re.fullmatch(r"commits/([0-9a-f]{40})/pulls", base_path):
            return self.pulls_by_commit.get(match[1], []) if page == 1 else []
        raise AssertionError(f"Unexpected GET {path}")

    def pages(self, path: str, key: str | None = None):
        mod = load_module("daily_test_release")
        return mod.GitHub.pages(self, path, key)


def green_jobs() -> list[dict]:
    return [
        {"name": "Build / Meson build profiles", "conclusion": "success"},
        {"name": "Meson tests / Meson tests (minimal environment)", "conclusion": "success"},
        {"name": "Debian package / Build .deb (ubuntu:26.04)", "conclusion": "success"},
    ]


def write_candidate_tree(root: Path, version: str = "0.4.2") -> Path:
    files = {
        "meson.build": f"project('cassini-talk', 'c',\n          version: '{version}',\n)\n",
        "debian/changelog": (
            f"cassini-talk ({version}) unstable; urgency=medium\n\n"
            "  * Prior release.\n\n"
            " -- Silvio Tomatis <silviot@gmail.com>  Sun, 08 Nov 2026 01:53:01 +0100\n"
        ),
        "build-aux/rpm/cassini-talk.spec": f"Name:           cassini-talk\nVersion:        {version}\nRelease:        1%{{?dist}}\n",
        "build-aux/arch/PKGBUILD": f"pkgname=cassini-talk\npkgver={version}\npkgrel=1\n",
        "NEWS": f"Version {version}\n~~~~~~~~~~~~~\nReleased: 2026-11-08\n\n- Stable notes.\n",
        "browser-extension/manifest.json": '{"version":"0.2.26"}\n',
        "data/cassini-talk.1": f'.TH CASSINI-TALK 1 "November 2026" "cassini-talk {version}" "User Commands"\n',
        "data/io.codemyriad.CassiniTalk.metainfo.xml.in": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<component>\n"
            "  <id>@APP_ID@</id>\n"
            "  <releases>\n"
            f'    <release version="{version}" date="2026-11-08"/>\n'
            "  </releases>\n"
            "</component>\n"
        ),
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def parse_event_filters(workflow_text: str, event_name: str) -> dict[str, list[str]]:
    on_block = workflow_text.split("\njobs:\n", 1)[0]
    match = re.search(rf"^  {re.escape(event_name)}:\n((?:    .*\n)+)", on_block, re.M)
    if match is None:
        return {}
    filters: dict[str, list[str]] = {}
    current_key: str | None = None
    for raw_line in match[1].splitlines():
        stripped = raw_line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if key_match := re.fullmatch(r"    ([a-z-]+):\s*(.*)", stripped):
            key, rest = key_match[1], key_match[2].strip()
            if rest.startswith("[") and rest.endswith("]"):
                items = [item.strip().strip("\"'") for item in rest[1:-1].split(",") if item.strip()]
                filters[key] = items
                current_key = None
            elif not rest:
                filters[key] = []
                current_key = key
            else:
                filters[key] = [rest.strip("\"'")]
                current_key = None
        elif current_key and (item_match := re.fullmatch(r"      -\s*(.+)", stripped)):
            filters[current_key].append(item_match[1].strip().strip("\"'"))
    for positive, negative in (("tags", "tags-ignore"), ("branches", "branches-ignore"), ("paths", "paths-ignore")):
        if positive in filters and negative in filters:
            raise ValueError(
                f'both "{positive}" and "{negative}" filters cannot be used for the same event "{event_name}"'
            )
    return filters


def github_ref_filter_matches(patterns: list[str], ref_name: str) -> bool:
    if not patterns or not any(not p.startswith("!") for p in patterns):
        raise ValueError("GitHub ref filter requires at least one positive pattern")

    def glob_re(pat: str) -> re.Pattern[str]:
        parts: list[str] = ["^"]
        idx = 0
        while idx < len(pat):
            if pat[idx : idx + 2] == "**":
                parts.append(".*")
                idx += 2
            elif pat[idx] == "*":
                parts.append("[^/]*")
                idx += 1
            elif pat[idx] == "?":
                parts.append("[^/]")
                idx += 1
            else:
                parts.append(re.escape(pat[idx]))
                idx += 1
        parts.append("$")
        return re.compile("".join(parts))

    matched = False
    for pat in patterns:
        if pat.startswith("!"):
            if glob_re(pat[1:]).fullmatch(ref_name):
                matched = False
        elif glob_re(pat).fullmatch(ref_name):
            matched = True
    return matched


def find_actionlint() -> str | None:
    found = shutil.which("actionlint")
    if found:
        return found
    fallback = Path.home() / "go/bin/actionlint"
    return str(fallback) if fallback.is_file() else None


def parse_schedule_entries(workflow_text: str) -> list[dict[str, str]]:
    on_block = workflow_text.split("\njobs:\n", 1)[0]
    match = re.search(r"^  schedule:\n((?:    .*\n)+)", on_block, re.M)
    if match is None:
        return []
    entries: list[dict[str, str]] = []
    for raw_line in match[1].splitlines():
        stripped = raw_line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if first := re.fullmatch(r"    -\s*([a-z-]+):\s*(.+)", stripped):
            entries.append({first[1]: first[2].strip().strip("\"'")})
        elif cont := re.fullmatch(r"      ([a-z-]+):\s*(.+)", stripped):
            entries[-1][cont[1]] = cont[2].strip().strip("\"'")
    return entries


def test_workflow_schedule_pinning_and_ci_push_unfiltered(tmp_path):
    daily_path = ROOT / ".github/workflows/daily-test-release.yml"
    release_path = ROOT / ".github/workflows/release.yml"
    daily = daily_path.read_text()
    assert parse_schedule_entries(daily) == [{"cron": "0 9,14,20 * * *", "timezone": "Europe/Berlin"}]
    assert "workflow_dispatch:" in daily
    assert "cancel-in-progress: false" in daily
    # Every checkout in daily-test-release.yml pins an exact commit SHA (never floating main).
    checkouts = [step for step in re.split(r"\n(?=      - )", daily) if "uses: actions/checkout@" in step]
    assert len(checkouts) == 6
    assert "ref: ${{ github.sha }}" in checkouts[0]
    for step in checkouts[1:]:
        assert "ref: ${{ needs.plan.outputs.sha }}" in step
    # Read-only cache restores only (zero PR cache-save debt).
    assert "uses: actions/cache@" not in daily
    assert daily.count("uses: actions/cache/restore@v6") == 9

    # Enforce GitHub Actions event-filter mutual exclusion across every repository workflow.
    for wf_path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        wf_text = wf_path.read_text()
        for ev in ("push", "pull_request", "pull_request_target"):
            parse_event_filters(wf_text, ev)

    # iOS #124 lesson: ci.yml must run on every push to main without path filters.
    ci_push = parse_event_filters((ROOT / ".github/workflows/ci.yml").read_text(), "push")
    assert ci_push == {"branches": ["main"]}

    # Production release.yml triggers on stable v* tags and excludes pre-release tags without tags-ignore conflict.
    release_wf = release_path.read_text()
    release_push = parse_event_filters(release_wf, "push")
    assert release_push == {"tags": ["v*", "!*-*"]}
    patterns = release_push["tags"]
    for stable_tag in ("v0.4.2", "v0.4.3", "v1.0.0", "v12.34.56"):
        assert github_ref_filter_matches(patterns, stable_tag), stable_tag
    for excluded_tag in ("v0.4.3-beta.1", "v0.4.3-beta.10", "v0.4.3-rc.1", "v1.0.0-alpha.1", "0.4.3", "nightly"):
        assert not github_ref_filter_matches(patterns, excluded_tag), excluded_tag

    mod = load_module("daily_test_release")
    bumped = mod.next_version("0.4.2", {"0.4.2"}, set())
    assert not github_ref_filter_matches(patterns, f"v{bumped['version_name']}")
    assert github_ref_filter_matches(patterns, f"v{bumped['base_version']}")

    broken_release = release_wf.replace('tags: ["v*", "!*-*"]', 'tags: ["v*"]\n    tags-ignore: ["*-*"]', 1)
    with pytest.raises(ValueError, match='both "tags" and "tags-ignore" filters cannot be used'):
        parse_event_filters(broken_release, "push")

    actionlint = find_actionlint()
    if actionlint is not None:
        cmd = [
            actionlint,
            "-color=false",
            "-ignore",
            r'label "ubuntu-26\.04" is unknown',
            "-ignore",
            "SC2010:",
        ]
        subprocess.run([*cmd, str(daily_path), str(release_path)], check=True)
        bad_wf = tmp_path / "bad-release.yml"
        bad_wf.write_text(broken_release, encoding="utf-8")
        proc = subprocess.run([*cmd, str(bad_wf)], capture_output=True, text=True, check=False)
        assert proc.returncode != 0
        assert 'both "tags" and "tags-ignore" filters cannot be used' in proc.stdout


def test_green_main_ci_rejects_fork_and_pr_runs_and_paginates():
    mod = load_module("daily_test_release")
    sha = "a" * 40
    untrusted_page_1 = [
        {
            "id": 500 + idx,
            "event": "pull_request",
            "head_branch": "main",
            "head_sha": sha,
            "head_repository": {"full_name": "attacker/cassini-gnome"},
            "status": "completed",
            "conclusion": "failure",
        }
        for idx in range(100)
    ]
    trusted_push_page_2 = [
        {
            "id": 200,
            "event": "push",
            "head_branch": "main",
            "head_sha": sha,
            "head_repository": {"full_name": "codemyriad/cassini-gnome"},
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://example.invalid/runs/200",
        }
    ]
    api = FakeGitHub(main_sha=sha, ci_pages=[untrusted_page_1, trusted_push_page_2], jobs_by_run={200: green_jobs()})
    assert mod.green_main_ci(api, sha) is True

    # Red push on main cannot be authorized by a passing pull_request run at the same SHA.
    red_push = dict(trusted_push_page_2[0], id=300, conclusion="failure")
    green_pr = {
        "id": 400,
        "event": "pull_request",
        "head_branch": "main",
        "head_sha": sha,
        "head_repository": {"full_name": "codemyriad/cassini-gnome"},
        "status": "completed",
        "conclusion": "success",
    }
    api_red = FakeGitHub(
        main_sha=sha, ci_pages=[[green_pr, red_push]], jobs_by_run={300: green_jobs(), 400: green_jobs()}
    )
    assert mod.green_main_ci(api_red, sha) is False

    # Newer failed workflow_dispatch CI run on main blocks admission even when earlier push passed.
    red_dispatch = dict(trusted_push_page_2[0], id=600, event="workflow_dispatch", conclusion="failure")
    api_dispatch = FakeGitHub(
        main_sha=sha, ci_pages=[[red_dispatch, trusted_push_page_2[0]]], jobs_by_run={200: green_jobs()}
    )
    assert mod.green_main_ci(api_dispatch, sha) is False

    # Scheduled nightly ci.yml runs probe unpinned rolling images outside merge and release gates:
    # a red schedule run does not block daily test releases when push passed, and a green schedule
    # run alone cannot authorize admission without a pinned push/workflow_dispatch run.
    red_schedule = dict(trusted_push_page_2[0], id=700, event="schedule", conclusion="failure")
    api_sched = FakeGitHub(
        main_sha=sha, ci_pages=[[red_schedule, trusted_push_page_2[0]]], jobs_by_run={200: green_jobs()}
    )
    assert mod.green_main_ci(api_sched, sha) is True

    only_green_schedule = dict(trusted_push_page_2[0], id=800, event="schedule", conclusion="success")
    api_only_sched = FakeGitHub(main_sha=sha, ci_pages=[[only_green_schedule]], jobs_by_run={800: green_jobs()})
    assert mod.green_main_ci(api_only_sched, sha) is False


def test_green_main_ci_accepts_two_distro_push_run_43174555655_while_stable_release_stays_strict():
    mod = load_module("daily_test_release")
    gm = load_module("green_main")
    sha = "fbaa2a357131a7009b11abaf6b7d83ae2eddeb4f"
    run_id = 43174555655
    url = f"https://github.com/codemyriad/cassini-gnome/actions/runs/{run_id}"
    # Verbatim job list from main push run 43174555655 (after #387 two-distro push gate).
    run_43174555655_jobs = [
        {"name": "Changed areas", "status": "completed", "conclusion": "success"},
        {"name": "Meson tests / Meson tests (minimal environment)", "status": "completed", "conclusion": "success"},
        {"name": "Library / golden-vectors-and-reader", "status": "completed", "conclusion": "success"},
        {
            "name": "Source / Build and test on ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96",
            "status": "completed",
            "conclusion": "success",
        },
        {"name": "Build / Meson build profiles", "status": "completed", "conclusion": "success"},
        {
            "name": "Source / Build and test on fedora:44@sha256:15da97ed9aa94dd3d92c3925eb89520f6c80e07ea7ee24cba848d1f8b6912b3e",
            "status": "completed",
            "conclusion": "success",
        },
        {
            "name": "RPM and Arch packages / Build .rpm (fedora:44@sha256:15da97ed9aa94dd3d92c3925eb89520f6c80e07ea7ee24cba848d1f8b6912b3e)",
            "status": "completed",
            "conclusion": "success",
        },
        {"name": "Source / Lint and test", "status": "completed", "conclusion": "success"},
        {
            "name": "Debian package / Build .deb (ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96)",
            "status": "completed",
            "conclusion": "success",
        },
        {"name": "RPM and Arch packages / Build Arch package", "status": "completed", "conclusion": "skipped"},
        {"name": "Flatpak", "status": "completed", "conclusion": "skipped"},
        {"name": "Offline / pinned-offline-recognition", "status": "completed", "conclusion": "success"},
        {"name": "Boards / current-window", "status": "completed", "conclusion": "success"},
        {"name": "Recording / capture", "status": "completed", "conclusion": "success"},
        {"name": "Recording / live-folder", "status": "completed", "conclusion": "success"},
        {"name": "Pytest coverage", "status": "completed", "conclusion": "success"},
        {"name": "Desktop / GNOME 50 / installed deb", "status": "completed", "conclusion": "success"},
    ]
    push_run = {
        "id": run_id,
        "event": "push",
        "head_branch": "main",
        "head_sha": sha,
        "head_repository": {"full_name": "codemyriad/cassini-gnome"},
        "status": "completed",
        "conclusion": "success",
        "html_url": url,
    }
    api_push = FakeGitHub(main_sha=sha, ci_pages=[[push_run]], jobs_by_run={run_id: run_43174555655_jobs})
    assert mod.green_main_ci(api_push, sha) is True

    # Stable tag validation still rejects the two-distro push run because Arch/Flatpak were skipped.
    with pytest.raises(ValueError, match="Every main job must succeed before release"):
        gm.validate_jobs(run_43174555655_jobs, url)

    # Full-matrix workflow_dispatch run (all jobs green) is accepted by both gates.
    full_jobs = [dict(job, conclusion="success") for job in run_43174555655_jobs]
    gm.validate_jobs(full_jobs, url)
    dispatch_run = dict(push_run, id=run_id + 100, event="workflow_dispatch")
    api_full = FakeGitHub(
        main_sha=sha,
        ci_pages=[[dispatch_run, push_run]],
        jobs_by_run={run_id: run_43174555655_jobs, run_id + 100: full_jobs},
    )
    assert mod.green_main_ci(api_full, sha) is True

    # Every non-skippable gate job in 43174555655 must succeed: skipping or failing any of them rejects the run.
    for idx, job in enumerate(run_43174555655_jobs):
        if job["conclusion"] == "success":
            for bad_conclusion in ("skipped", "failure", "cancelled"):
                mutated = [
                    dict(j, conclusion=bad_conclusion) if i == idx else j for i, j in enumerate(run_43174555655_jobs)
                ]
                api_bad = FakeGitHub(main_sha=sha, ci_pages=[[push_run]], jobs_by_run={run_id: mutated})
                assert mod.green_main_ci(api_bad, sha) is False, (job["name"], bad_conclusion)
        else:
            # Skippable rolling/packaging legs may be skipped, never failed, cancelled, or incomplete.
            for bad in (
                {"conclusion": "failure"},
                {"conclusion": "cancelled"},
                {"status": "in_progress", "conclusion": None},
            ):
                mutated = [dict(j, **bad) if i == idx else j for i, j in enumerate(run_43174555655_jobs)]
                api_bad = FakeGitHub(main_sha=sha, ci_pages=[[push_run]], jobs_by_run={run_id: mutated})
                assert mod.green_main_ci(api_bad, sha) is False, (job["name"], bad)


def test_version_bump_advances_past_released_tags_and_reserves_drafts_and_failures():
    mod = load_module("daily_test_release")
    # Android #181 lesson: 0.4.2 is already released -> unattended betas start at 0.4.3-beta.1.
    v1 = mod.next_version("0.4.2", {"0.4.0", "0.4.1", "0.4.2"}, set())
    assert v1 == {
        "base_version": "0.4.3",
        "beta": 1,
        "version_name": "0.4.3-beta.1",
        "package_version": "0.4.3~beta.1",
        "arch_pkgver": "0.4.3beta1",
    }
    # Failed reservations and drafts increment beta without skipping ahead when meson.build is bumped to unreleased 0.4.3.
    v3 = mod.next_version("0.4.3", {"0.4.2"}, {("0.4.3", 1), ("0.4.3", 2)})
    assert v3["version_name"] == "0.4.3-beta.3"
    assert v3["package_version"] == "0.4.3~beta.3"
    assert v3["arch_pkgver"] == "0.4.3beta3"

    if shutil.which("dpkg"):
        for older, newer in (
            ("0.4.2", "0.4.3~beta.1"),
            ("0.4.3~beta.1", "0.4.3~beta.2"),
            ("0.4.3~beta.2", "0.4.3~beta.10"),
            ("0.4.3~beta.10", "0.4.3"),
        ):
            subprocess.run(["dpkg", "--compare-versions", older, "lt", newer], check=True)


def test_deployments_and_hotfix_tags_never_wedge_on_deleted_runs_or_non_ancestors(tmp_path):
    mod = load_module("daily_test_release")
    c0, c1, c2 = "1" * 40, "2" * 40, "3" * 40
    hotfix = "9" * 40
    write_candidate_tree(tmp_path, "0.4.2")
    git = FakeGit(
        head=c2,
        commits=[c0, c1, c2],
        tags={"v0.4.2": c0, "v0.4.3": hotfix},
        side_commits={hotfix: c1},
    )
    ci_ok = [
        {
            "id": 10,
            "event": "push",
            "head_branch": "main",
            "head_sha": c2,
            "head_repository": {"full_name": "codemyriad/cassini-gnome"},
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://example.invalid/runs/10",
        }
    ]
    # Deployment 52 failed (run deleted >90 days ago); Deployment 51 lost status ack (404 on release tag);
    # Deployment 50 succeeded at c1. None may query actions/runs/*/jobs for 52/51/50 or wedge plan().
    deployments = [
        {"id": 52, "sha": c2, "payload": {"schema": 1, "version_name": "0.4.4-beta.2", "run_id": 999999}},
        {"id": 51, "sha": c2, "payload": {"schema": 1, "version_name": "0.4.4-beta.1", "run_id": 888888}},
        {"id": 50, "sha": c1, "payload": {"schema": 1, "version_name": "0.4.3-beta.1", "run_id": 777777}},
    ]
    statuses = {52: [{"state": "failure"}], 51: [], 50: [{"state": "success"}]}
    pulls = {
        c2: [
            {
                "number": 385,
                "title": "Fix <Spectrogram> & `ticks` @alice",
                "merged_at": "2026-11-12T01:00:00Z",
                "base": {"ref": "main"},
                "merge_commit_sha": c2,
            }
        ]
    }
    api = FakeGitHub(
        main_sha=c2,
        ci_pages=[ci_ok],
        jobs_by_run={10: green_jobs()},
        deployments=deployments,
        statuses_by_dep=statuses,
        pulls_by_commit=pulls,
    )
    planned = mod.plan(api, c2, tmp_path, 43210, git=git)
    assert planned["build"] is True
    assert planned["baseline"] == c1
    # Both v0.4.2 and hotfix tag v0.4.3 exist, and 0.4.4-beta.1..2 were reserved -> 0.4.4-beta.3!
    assert planned["version_name"] == "0.4.4-beta.3"
    assert "``Fix <Spectrogram> & `ticks` @alice`` (#385)" in planned["notes"]
    assert not any("actions/runs/999999" in req or "actions/runs/888888" in req for req in api.requests)

    # Unchanged main (head == latest baseline) skips cleanly without reserving a deployment.
    git_same = FakeGit(head=c1, commits=[c0, c1], tags={"v0.4.2": c0})
    ci_c1 = [dict(ci_ok[0], head_sha=c1)]
    api_same = FakeGitHub(
        main_sha=c1,
        ci_pages=[ci_c1],
        jobs_by_run={10: green_jobs()},
        deployments=[deployments[2]],
        statuses_by_dep={50: [{"state": "success"}]},
    )
    skipped = mod.plan(api_same, c1, tmp_path, 43211, git=git_same)
    assert skipped == {"build": False, "reason": "main is unchanged since the last test release"}
    assert api_same.posts == []


def test_literal_code_span_and_notes_bounds():
    mod = load_module("daily_test_release")
    assert mod.literal_code_span("Fix StateFlow<UiState>\n\tand [link](https://x)") == (
        "`Fix StateFlow<UiState> and [link](https://x)`"
    )
    assert mod.literal_code_span("`leading and ``double`` backticks`") == ("``` `leading and ``double`` backticks` ```")
    assert mod.literal_code_span("\x00\x07  ") == "`(untitled)`"

    c0, c1 = "a" * 40, "b" * 40
    git = FakeGit(head=c1, commits=[c0, c1])
    huge_pulls = {
        c1: [
            {
                "number": idx,
                "title": f"PR {idx} 🚀 " + ("word " * 80),
                "merged_at": "2026-11-12T01:00:00Z",
                "base": {"ref": "main"},
                "merge_commit_sha": c1,
            }
            for idx in range(1, 300)
        ]
    }
    api = FakeGitHub(main_sha=c1, pulls_by_commit=huge_pulls)
    rn = load_module("release_notes")
    notes = mod.build_notes(api, c0, c1, git)
    encoded = notes.encode("utf-8")
    assert len(encoded) <= mod.MAX_NOTES_BYTES
    assert notes.startswith(rn.EMPTY_USER_NOTES + "\n\n<details>\n<summary>Developer changelog</summary>\n\n")
    assert notes.endswith("\n- ...\n</details>\n")


def test_user_release_notes_fragments_grouping_overflow_tracking_and_anti_leak():
    mod = load_module("daily_test_release")
    rn = load_module("release_notes")

    # 1. Every committed fragment in release-notes.d/ parses cleanly and renders within the 500-char store limit.
    repo_fragments = rn.load_fragments_dir(ROOT / "release-notes.d")
    assert len(repo_fragments) >= 5
    rendered_repo = rn.render_user_notes(repo_fragments)
    assert len(rendered_repo) <= rn.USER_NOTES_LIMIT == 500
    rn.validate_user_text(rendered_repo)
    assert rendered_repo.index("New\n") < rendered_repo.index("Better\n") < rendered_repo.index("Fixed\n")

    # 2. Empty unpublished fragment set produces the exact plain fallback text.
    assert rn.render_user_notes([]) == (
        "Fixes and improvements under the hood. Nothing changes in how you use Cassini."
    )

    # 3. Grouping order (New -> Better -> Fixed) and >500-character overflow keeping 'new' items + 'and smaller fixes'.
    fixture_fragments = [
        ("fixed", "Exported meetings keep their original recording date when opened in another time zone."),
        ("better", "Search in the Library matches meeting titles even before a recording is written down."),
        ("new", "Import several meeting files or a ZIP archive into your Library at once."),
        ("new", "Export your entire Library or selected meetings as a single ZIP archive."),
        ("better", "Long transcripts scroll smoothly without jumping while you read or play back."),
        ("better", "Parakeet Ultra is used automatically once downloaded, while Whistle stays built in."),
        ("fixed", "Saved voices keep their sample playback position steady when you switch speakers."),
        ("fixed", "Stopping a recording from the top bar updates the window title immediately."),
    ]
    overflowed = rn.render_user_notes(fixture_fragments)
    assert len(overflowed) <= 500
    assert overflowed.startswith(
        "New\n"
        "- Import several meeting files or a ZIP archive into your Library at once.\n"
        "- Export your entire Library or selected meetings as a single ZIP archive."
    )
    assert overflowed.endswith("\n\nand smaller fixes")
    rn.validate_user_text(overflowed)

    # 4. Forbidden developer markers can never reach user-facing text.
    for bad_body in (
        "Fix transcript jump in #390.",
        "Align export contract with D84 parity.",
        "Speed up CI package verification.",
        "Fold merged PR titles under details.",
        "Please reload your browser tab after updating.",
        "Cassini's Library keeps your meetings.",
        "Update build-aux/ci/daily_test_release.py notes.",
        "Not released yet.",
    ):
        with pytest.raises(ValueError):
            rn.parse_fragment(f"kind: fixed\n---\n{bad_body}\n", name="release-notes.d/bad-entry.md")
    with pytest.raises(ValueError, match="filename"):
        rn.parse_fragment("kind: new\n---\nValid sentence.\n", name="release-notes.d/Bad_Name.md")

    # 5. Baseline tracking excludes already-published fragments and keeps PR titles strictly inside <details>.
    c0, c1, c2 = "1" * 40, "2" * 40, "3" * 40
    frag_old = "kind: new\n---\nAlready shipped in the previous test build.\n"
    frag_new = "kind: better\n---\nSearch in the Library matches meeting titles.\n"
    git = FakeGit(
        head=c2,
        commits=[c0, c1, c2],
        fragments_by_commit={
            c1: {"release-notes.d/already-shipped.md": frag_old},
            c2: {
                "release-notes.d/already-shipped.md": frag_old,
                "release-notes.d/search-titles.md": frag_new,
            },
        },
    )
    pulls = {
        c2: [
            {
                "number": 389,
                "title": "feat(library): search meeting titles with D95 CI gate in window.py",
                "merged_at": "2026-11-12T02:00:00Z",
                "base": {"ref": "main"},
                "merge_commit_sha": c2,
            }
        ]
    }
    api = FakeGitHub(main_sha=c2, pulls_by_commit=pulls)
    user_only = mod.build_user_notes(c1, c2, git)
    full_body = mod.build_notes(api, c1, c2, git, user_notes=user_only)

    assert "Already shipped" not in user_only
    assert user_only == "Better\n- Search in the Library matches meeting titles."
    assert len(user_only) <= 500
    rn.validate_user_text(user_only)

    visible_top, sep, folded_dev = full_body.partition("\n\n<details>\n<summary>Developer changelog</summary>\n\n")
    assert sep
    assert visible_top == user_only
    rn.validate_user_text(visible_top)
    assert "#389" not in visible_top and "D95" not in visible_top and "CI" not in visible_top
    assert "feat(library): search meeting titles with D95 CI gate in window.py" in folded_dev
    assert "(#389)" in folded_dev


def test_apply_patches_all_six_files_and_arch_build_script_accepts_prerelease(tmp_path):
    mod = load_module("daily_test_release")
    meta_mod = load_module("release_metadata")
    rel_test = importlib.util.spec_from_file_location("test_ci_release", ROOT / "tests/test_ci_release.py")
    assert rel_test and rel_test.loader
    rel_module = importlib.util.module_from_spec(rel_test)
    rel_test.loader.exec_module(rel_module)

    sha = "d" * 40
    write_candidate_tree(tmp_path, "0.4.2")
    git = FakeGit(head=sha, commits=[sha], timestamp=1794470400)
    plan_data = {
        "build": True,
        "sha": sha,
        "baseline": "c" * 40,
        "deployment_id": 901,
        "schema": 1,
        "version_name": "0.4.3-beta.1",
        "package_version": "0.4.3~beta.1",
        "arch_pkgver": "0.4.3beta1",
        "base_version": "0.4.3",
        "beta": 1,
        "run_id": 123,
        "notes": f"Source: `{sha}`\n\n- `Fix audio` (#385)\n",
    }
    mod.apply_plan(plan_data, sha, tmp_path, git=git)

    # Pre-release check passes; production release_metadata.check still rejects ~beta.N!
    checked = meta_mod.check_prerelease(tmp_path, "0.4.3-beta.1")
    assert checked["packageVersion"] == "0.4.3~beta.1"
    assert checked["archVersion"] == "0.4.3beta1"
    assert checked["newsVersion"] == "0.4.2"
    with pytest.raises(ValueError):
        meta_mod.check(tmp_path)
    assert rel_module.news_version(tmp_path) == "0.4.2"

    # Verify build-aux/arch/build.sh version check accepts 0.4.3~beta.1 <-> 0.4.3beta1 and rejects drift.
    arch_script = (ROOT / "build-aux/arch/build.sh").read_text()
    assert "sed 's/~beta\\.\\([1-9][0-9]*\\)$/beta\\1/'" in arch_script
    if shutil.which("sh"):
        snippet = "\n".join(arch_script.splitlines()[9:17]) + "\n"
        subprocess.run(["sh", "-eu", "-c", snippet], cwd=tmp_path, check=True)
        (tmp_path / "build-aux/arch/PKGBUILD").write_text("pkgname=cassini-talk\npkgver=0.4.3beta2\npkgrel=1\n")
        assert subprocess.run(["sh", "-eu", "-c", snippet], cwd=tmp_path, capture_output=True).returncode != 0


def test_publish_stages_verifies_draft_undrafts_and_verifies_downloaded_sums(tmp_path):
    mod = load_module("daily_test_release")
    sha = "e" * 40
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    plan_data = {
        "build": True,
        "sha": sha,
        "baseline": "c" * 40,
        "deployment_id": 902,
        "schema": 1,
        "version_name": "0.4.3-beta.2",
        "package_version": "0.4.3~beta.2",
        "arch_pkgver": "0.4.3beta2",
        "base_version": "0.4.3",
        "beta": 2,
        "run_id": 456,
        "notes": f"Source: `{sha}`\n\n- `Daily pipeline` (#387)\n",
    }
    (plan_dir / "test-release-plan.json").write_text(json.dumps(plan_data))
    metadata = mod.plan_metadata(plan_data)

    # Unbumped 0.4.2 artifacts must be refused before any gh release call!
    bad_artifacts = tmp_path / "bad_artifacts"
    bad_artifacts.mkdir()
    for name in (
        "cassini-talk_0.4.2~ubuntu26.04_amd64.deb",
        "cassini-talk_0.4.2~debian-testing_amd64.deb",
        "cassini-talk-0.4.2-1.fc44.x86_64.rpm",
        "cassini-talk-0.4.2-1.opensuse_tumbleweed.x86_64.rpm",
        "cassini-talk-0.4.2-1-x86_64.pkg.tar.zst",
    ):
        (bad_artifacts / name).write_bytes(name.encode())
    calls: list[list[str]] = []
    api = FakeGitHub(main_sha=sha)
    with pytest.raises(ValueError, match="do not match"):
        mod.publish_release(
            api,
            metadata,
            plan_dir,
            bad_artifacts,
            tmp_path / "bad_work",
            sha,
            run=lambda cmd, **_kw: calls.append(list(cmd)),
        )
    assert calls == []

    # Valid bumped artifacts go draft -> verify -> undraft -> download & SHA256SUMS verify.
    good_artifacts = tmp_path / "good_artifacts"
    good_artifacts.mkdir()
    for name in (
        "cassini-talk_0.4.3~beta.2~ubuntu26.04_amd64.deb",
        "cassini-talk_0.4.3~beta.2~debian-testing_amd64.deb",
        "cassini-talk-0.4.3~beta.2-1.fc44.x86_64.rpm",
        "cassini-talk-0.4.3~beta.2-1.opensuse_tumbleweed.x86_64.rpm",
        "cassini-talk-0.4.3beta2-1-x86_64.pkg.tar.zst",
    ):
        (good_artifacts / name).write_bytes(f"payload-{name}".encode())

    uploaded_files: list[Path] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "release", "create"]:
            assert "--draft" in cmd and "--prerelease" in cmd and "--latest=false" in cmd
            uploaded_files.extend(Path(p) for p in cmd[-6:])
            api.releases = [
                {
                    "id": 1,
                    "tag_name": "v0.4.3-beta.2",
                    "name": "nightly — 0.4.3-beta.2",
                    "draft": True,
                    "prerelease": True,
                    "target_commitish": sha,
                    "assets": [{"name": p.name, "size": p.stat().st_size, "state": "uploaded"} for p in uploaded_files],
                }
            ]
        elif cmd[:3] == ["gh", "release", "edit"]:
            assert "--draft=false" in cmd and "--prerelease" in cmd and "--latest=false" in cmd
            api.releases[0]["draft"] = False
        elif cmd[:3] == ["gh", "release", "download"]:
            dest = Path(cmd[cmd.index("--dir") + 1])
            for path in uploaded_files:
                shutil.copyfile(path, dest / path.name)

    mod.publish_release(api, metadata, plan_dir, good_artifacts, tmp_path / "good_work", sha, run=fake_run)
    assert [c[:3] for c in calls] == [
        ["gh", "release", "create"],
        ["gh", "release", "edit"],
        ["gh", "release", "download"],
    ]
    assert api.releases[0]["draft"] is False

    # Incomplete draft upload must abort BEFORE undrafting!
    calls.clear()
    api_partial = FakeGitHub(main_sha=sha)

    def partial_run(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "release", "create"]:
            files = [Path(p) for p in cmd[-6:]]
            api_partial.releases = [
                {
                    "id": 2,
                    "tag_name": "v0.4.3-beta.2",
                    "draft": True,
                    "prerelease": True,
                    "target_commitish": sha,
                    "assets": [{"name": p.name, "size": p.stat().st_size, "state": "uploaded"} for p in files[:5]],
                }
            ]

    with pytest.raises(ValueError, match="incomplete"):
        mod.publish_release(
            api_partial, metadata, plan_dir, good_artifacts, tmp_path / "partial_work", sha, run=partial_run
        )
    assert [c[:3] for c in calls] == [["gh", "release", "create"]]


def test_finish_records_actual_publication_even_when_post_step_fails(tmp_path, monkeypatch, capsys):
    mod = load_module("daily_test_release")
    sha = "f" * 40
    meta = {"version_name": "0.4.3-beta.1", "deployment_id": 910, "sha": sha}
    live_release = {
        "draft": False,
        "prerelease": True,
        "assets": [{"name": f"a{i}", "state": "uploaded"} for i in range(6)],
    }

    # Post-step failure after release went live still records deployment 'success' so next slot won't duplicate.
    api_live = FakeGitHub(main_sha=sha, tags_by_name={"v0.4.3-beta.1": live_release})
    ok, msg = mod.finish(api_live, meta, {"publish": "failure"}, "https://example.invalid/runs/1")
    assert ok is False and "after v0.4.3-beta.1 went live" in msg
    assert api_live.posts == [
        (
            "deployments/910/statuses",
            {"state": "success", "log_url": "https://example.invalid/runs/1", "auto_inactive": False},
        )
    ]

    # Missing release (HTTP 404) records deployment 'failure' and names the failed job in one line.
    api_fail = FakeGitHub(main_sha=sha)
    ok2, msg2 = mod.finish(
        api_fail, meta, {"deb": "success", "rpm": "failure", "arch": "success", "publish": "skipped"}, "https://x/2"
    )
    assert ok2 is False and msg2 == "failed: rpm=failure, publish=skipped (https://x/2)"
    assert api_fail.posts[0][1]["state"] == "failure"

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    mod.report("failed: rpm=failure\nsecond line", failed=True)
    out = capsys.readouterr().out
    assert out == "::error::[GNOME] daily test release failed: rpm=failure second line\n"
    assert summary.read_text() == "[GNOME] daily test release failed: rpm=failure second line\n"
