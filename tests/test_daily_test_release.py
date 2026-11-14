# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract and regression tests for unattended GNOME daily test releases."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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
        commits_by_sha: dict[str, dict | None] | None = None,
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
        self.commits_by_sha = commits_by_sha or {}
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
        if match := re.fullmatch(r"commits/([0-9a-f]{40})", base_path):
            sha = match[1]
            if sha in self.commits_by_sha:
                commit_obj = self.commits_by_sha[sha]
                if commit_obj is not None:
                    return commit_obj
                if allow_404:
                    return None
                raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)")
            return {"sha": sha, "parents": []}
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
        "browser-extension/manifest.json": '{"version":"0.2.31"}\n',
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


def test_green_main_ci_accepts_identical_tree_pr_desktop_skips_and_rejects_unverified_skips():
    mod = load_module("daily_test_release")
    merge_sha = "1" * 40
    base_sha = "2" * 40
    pr_sha = "3" * 40
    same_tree = "4" * 40
    diff_tree = "5" * 40
    pr_merge_sha = "6" * 40
    release_base_sha = "7" * 40
    release_merge_sha = "8" * 40

    push_skipped_desktop_jobs = [
        {"name": "Changed areas", "status": "completed", "conclusion": "success"},
        {"name": "Meson tests / Meson tests (minimal environment)", "status": "completed", "conclusion": "success"},
        {"name": "Build / Meson build profiles", "status": "completed", "conclusion": "success"},
        {"name": "Library / vectors", "status": "completed", "conclusion": "success"},
        {"name": "Source / Lint and test", "status": "completed", "conclusion": "success"},
        {
            "name": "Source / Build and test on fedora:44@sha256:" + "a" * 64,
            "status": "completed",
            "conclusion": "success",
        },
        {
            "name": "Debian package / Build .deb (ubuntu:26.04@sha256:" + "b" * 64 + ")",
            "status": "completed",
            "conclusion": "success",
        },
        {
            "name": "RPM and Arch packages / Build .rpm (fedora:44@sha256:" + "a" * 64 + ")",
            "status": "completed",
            "conclusion": "success",
        },
        {"name": "RPM and Arch packages / Build Arch package", "status": "completed", "conclusion": "skipped"},
        {"name": "Flatpak", "status": "completed", "conclusion": "skipped"},
        {"name": "Pytest coverage", "status": "completed", "conclusion": "success"},
        {"name": "Recording", "status": "completed", "conclusion": "skipped"},
        {"name": "Boards", "status": "completed", "conclusion": "skipped"},
        {"name": "Offline", "status": "completed", "conclusion": "skipped"},
        {"name": "Desktop", "status": "completed", "conclusion": "skipped"},
    ]
    pr_green_jobs = [
        {"name": "Build / Meson build profiles", "status": "completed", "conclusion": "success"},
        {"name": "Meson tests / Meson tests (minimal environment)", "status": "completed", "conclusion": "success"},
        {"name": "Recording / capture", "status": "completed", "conclusion": "success"},
        {"name": "Recording / live-folder", "status": "completed", "conclusion": "success"},
        {"name": "Boards / current-window", "status": "completed", "conclusion": "success"},
        {"name": "Offline / pinned-offline-recognition", "status": "completed", "conclusion": "success"},
        {"name": "Desktop / GNOME 50 / installed deb", "status": "completed", "conclusion": "success"},
    ]
    push_run = {
        "id": 200,
        "event": "push",
        "head_branch": "main",
        "head_sha": merge_sha,
        "head_repository": {"full_name": "codemyriad/cassini-gnome"},
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example.invalid/runs/200",
    }
    pr_run = {
        "id": 100,
        "event": "pull_request",
        "head_branch": "ci/feature",
        "head_sha": pr_sha,
        "head_repository": {"full_name": "codemyriad/cassini-gnome"},
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example.invalid/runs/100",
        "referenced_workflows": [
            {
                "path": f"codemyriad/cassini-gnome/.github/workflows/meson-tests.yml@{pr_merge_sha}",
                "ref": "refs/pull/42/merge",
                "sha": pr_merge_sha,
            },
            {
                "path": f"codemyriad/cassini-gnome/.github/workflows/build.yml@{pr_merge_sha}",
                "ref": "refs/pull/42/merge",
                "sha": pr_merge_sha,
            },
        ],
    }
    identical_commits = {
        merge_sha: {
            "sha": merge_sha,
            "parents": [{"sha": base_sha}, {"sha": pr_sha}],
            "commit": {"tree": {"sha": same_tree}},
        },
        pr_sha: {"sha": pr_sha, "parents": [{"sha": base_sha}], "commit": {"tree": {"sha": same_tree}}},
        pr_merge_sha: {
            "sha": pr_merge_sha,
            "parents": [{"sha": base_sha}, {"sha": pr_sha}],
            "commit": {"tree": {"sha": same_tree}},
        },
    }

    # 1. Identical tree + all 4 PR desktop areas green -> accepted!
    api_ok = FakeGitHub(
        main_sha=merge_sha,
        ci_pages=[[push_run, pr_run]],
        jobs_by_run={200: push_skipped_desktop_jobs, 100: pr_green_jobs},
        commits_by_sha=identical_commits,
    )
    assert mod.green_main_ci(api_ok, merge_sha) is True

    # 2. Newer no-op labeled PR runs (either conclusion='skipped' or conclusion='success' with all jobs skipped)
    # do NOT mask the earlier green PR run on pr_sha.
    for noop_conclusion in ("skipped", "success"):
        noop_run = dict(pr_run, id=150, conclusion=noop_conclusion)
        noop_jobs = [{"name": "Changed areas", "status": "completed", "conclusion": "skipped"}]
        api_noop = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, noop_run, pr_run]],
            jobs_by_run={200: push_skipped_desktop_jobs, 150: noop_jobs, 100: pr_green_jobs},
            commits_by_sha=identical_commits,
        )
        assert mod.green_main_ci(api_noop, merge_sha) is True, noop_conclusion

    # 3. PR path-filtered Boards (skipped in PR): main push skipping Boards is REJECTED,
    # whereas main push running Boards green while skipping Recording/Offline/Desktop is ACCEPTED.
    pr_without_boards = [
        {"name": "Boards", "status": "completed", "conclusion": "skipped"}
        if j["name"] == "Boards / current-window"
        else j
        for j in pr_green_jobs
    ]
    api_unverified_boards = FakeGitHub(
        main_sha=merge_sha,
        ci_pages=[[push_run, pr_run]],
        jobs_by_run={200: push_skipped_desktop_jobs, 100: pr_without_boards},
        commits_by_sha=identical_commits,
    )
    assert mod.green_main_ci(api_unverified_boards, merge_sha) is False
    push_ran_boards = [
        {"name": "Boards / current-window", "status": "completed", "conclusion": "success"}
        if j["name"] == "Boards"
        else j
        for j in push_skipped_desktop_jobs
    ]
    api_push_boards = FakeGitHub(
        main_sha=merge_sha,
        ci_pages=[[push_run, pr_run]],
        jobs_by_run={200: push_ran_boards, 100: pr_without_boards},
        commits_by_sha=identical_commits,
    )
    assert mod.green_main_ci(api_push_boards, merge_sha) is True

    # 4. Tree mismatch, 1-parent squash commit, missing commit (404), fork PR, or newer failed/pending PR run -> REJECTED.
    skew_commits = {
        **identical_commits,
        pr_sha: {"sha": pr_sha, "parents": [{"sha": base_sha}], "commit": {"tree": {"sha": diff_tree}}},
    }
    squash_commits = {
        merge_sha: {"sha": merge_sha, "parents": [{"sha": base_sha}], "commit": {"tree": {"sha": same_tree}}}
    }
    for bad_commits in (skew_commits, squash_commits, {merge_sha: None}):
        api_bad_tree = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, pr_run]],
            jobs_by_run={200: push_skipped_desktop_jobs, 100: pr_green_jobs},
            commits_by_sha=bad_commits,
        )
        assert mod.green_main_ci(api_bad_tree, merge_sha) is False

    fork_pr_run = dict(pr_run, head_repository={"full_name": "attacker/cassini-gnome"})
    api_fork = FakeGitHub(
        main_sha=merge_sha,
        ci_pages=[[push_run, fork_pr_run]],
        jobs_by_run={200: push_skipped_desktop_jobs, 100: pr_green_jobs},
        commits_by_sha=identical_commits,
    )
    assert mod.green_main_ci(api_fork, merge_sha) is False

    for newer_bad in (
        dict(pr_run, id=160, conclusion="failure"),
        dict(pr_run, id=160, status="in_progress", conclusion=None),
    ):
        api_superseded = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, newer_bad, pr_run]],
            jobs_by_run={200: push_skipped_desktop_jobs, 160: pr_green_jobs, 100: pr_green_jobs},
            commits_by_sha=identical_commits,
        )
        assert mod.green_main_ci(api_superseded, merge_sha) is False

    # 5. Codex #415: identical PR head SHA whose green PR run targeted another branch (e.g. release)
    # and therefore checked out a different synthetic merge tree (diff_tree != same_tree) MUST be
    # rejected — both alone and when newer than an earlier main-targeted PR run!
    release_pr_run = dict(
        pr_run,
        id=170,
        referenced_workflows=[
            {
                "path": f"codemyriad/cassini-gnome/.github/workflows/meson-tests.yml@{release_merge_sha}",
                "ref": "refs/pull/99/merge",
                "sha": release_merge_sha,
            }
        ],
    )
    release_commits = {
        **identical_commits,
        release_merge_sha: {
            "sha": release_merge_sha,
            "parents": [{"sha": release_base_sha}, {"sha": pr_sha}],
            "commit": {"tree": {"sha": diff_tree}},
        },
    }
    for pr_runs in ([release_pr_run], [release_pr_run, pr_run]):
        api_release_tree = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, *pr_runs]],
            jobs_by_run={200: push_skipped_desktop_jobs, 170: pr_green_jobs, 100: pr_green_jobs},
            commits_by_sha=release_commits,
        )
        assert mod.green_main_ci(api_release_tree, merge_sha) is False

    # Unavailable or malformed checkout-tree evidence also fails closed.
    other_sha = "9" * 40
    for bad_run, bad_commits in (
        ({k: v for k, v in pr_run.items() if k != "referenced_workflows"}, identical_commits),
        (dict(pr_run, referenced_workflows=[]), identical_commits),
        (
            dict(
                pr_run,
                referenced_workflows=[
                    pr_run["referenced_workflows"][0],
                    {
                        "path": f"codemyriad/cassini-gnome/.github/workflows/build.yml@{other_sha}",
                        "ref": "refs/pull/42/merge",
                        "sha": other_sha,
                    },
                ],
            ),
            identical_commits,
        ),
        (
            dict(
                pr_run,
                referenced_workflows=[
                    {
                        "path": f"codemyriad/cassini-gnome/.github/workflows/meson-tests.yml@{pr_merge_sha}",
                        "ref": "refs/heads/release",
                        "sha": pr_merge_sha,
                    }
                ],
            ),
            identical_commits,
        ),
        (
            dict(
                pr_run,
                referenced_workflows=[
                    {
                        "path": f"attacker/cassini-gnome/.github/workflows/meson-tests.yml@{pr_merge_sha}",
                        "ref": "refs/pull/42/merge",
                        "sha": pr_merge_sha,
                    }
                ],
            ),
            identical_commits,
        ),
        (pr_run, {**identical_commits, pr_merge_sha: None}),
        (
            pr_run,
            {
                **identical_commits,
                pr_merge_sha: {
                    "sha": pr_merge_sha,
                    "parents": [{"sha": base_sha}, {"sha": other_sha}],
                    "commit": {"tree": {"sha": same_tree}},
                },
            },
        ),
    ):
        api_bad_evidence = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, bad_run]],
            jobs_by_run={200: push_skipped_desktop_jobs, 100: pr_green_jobs},
            commits_by_sha=bad_commits,
        )
        assert mod.green_main_ci(api_bad_evidence, merge_sha) is False

    # 6. Non-desktop gate jobs (`Pytest coverage`, `Library / vectors`, `Meson tests`) can NEVER be skipped.
    for required_name in ("Pytest coverage", "Library / vectors", "Meson tests / Meson tests (minimal environment)"):
        bad_push_jobs = [
            dict(j, conclusion="skipped") if j["name"] == required_name else j for j in push_skipped_desktop_jobs
        ]
        api_bad_gate = FakeGitHub(
            main_sha=merge_sha,
            ci_pages=[[push_run, pr_run]],
            jobs_by_run={200: bad_push_jobs, 100: pr_green_jobs},
            commits_by_sha=identical_commits,
        )
        assert mod.green_main_ci(api_bad_gate, merge_sha) is False, required_name


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


def test_user_release_notes_fragments_grouping_overflow_tracking_and_anti_leak(tmp_path):
    mod = load_module("daily_test_release")
    rn = load_module("release_notes")

    # 1. Every committed fragment in release-notes.d/ (including 0 after stable cleanup, or omitted groups)
    #    parses cleanly and renders within the 500-char store limit.
    repo_fragments = rn.load_fragments_dir(ROOT / "release-notes.d")
    rendered_repo = rn.render_user_notes(repo_fragments)
    assert len(rendered_repo) <= rn.USER_NOTES_LIMIT == 500
    rn.validate_user_text(rendered_repo)

    plus_one = [*repo_fragments, ("new", "Open any audio file from your computer directly into Cassini.")]
    rendered_plus_one = rn.render_user_notes(plus_one)
    assert len(rendered_plus_one) <= rn.USER_NOTES_LIMIT == 500
    rn.validate_user_text(rendered_plus_one)

    # 2. Empty unpublished fragment set (e.g. after stable-release cleanup) produces the exact plain fallback text.
    empty_dir = tmp_path / "empty-release-notes.d"
    empty_dir.mkdir()
    assert rn.load_fragments_dir(empty_dir) == []
    assert rn.render_user_notes([]) == (
        "Fixes and improvements under the hood. Nothing changes in how you use Cassini."
    )

    # 3. Fixed fixtures verify grouping order (New -> Better -> Fixed) and >500-char overflow truncation.
    short_fixture = [
        ("fixed", "Saved voices keep their sample position steady."),
        ("better", "Search in the Library matches meeting titles."),
        ("new", "Export your Library as a single ZIP archive."),
    ]
    grouped_short = rn.render_user_notes(short_fixture)
    assert grouped_short.index("New\n") < grouped_short.index("Better\n") < grouped_short.index("Fixed\n")
    rn.validate_user_text(grouped_short)

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
    assert "Fixed\n" not in overflowed
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


def test_plan_waits_bounded_for_in_progress_main_ci_and_keeps_admitted_sha(tmp_path):
    mod = load_module("daily_test_release")
    daily_wf = (ROOT / ".github/workflows/daily-test-release.yml").read_text()
    assert "timeout-minutes: 55" in daily_wf

    c0, c1, c2 = "1" * 40, "2" * 40, "3" * 40
    write_candidate_tree(tmp_path, "0.4.2")
    git = FakeGit(head=c1, commits=[c0, c1], tags={"v0.4.2": c0})

    def make_run(run_id: int, status: str, conclusion: str | None, *, event: str = "push") -> dict:
        return {
            "id": run_id,
            "event": event,
            "head_branch": "main",
            "head_sha": c1,
            "head_repository": {"full_name": "codemyriad/cassini-gnome"},
            "status": status,
            "conclusion": conclusion,
            "html_url": f"https://example.invalid/runs/{run_id}",
        }

    # 1. Queued -> in_progress -> completed/success waits and reserves the admitted SHA,
    #    even when commits/main advances to c2 while waiting.
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    api_ok = FakeGitHub(main_sha=c1, ci_pages=[[make_run(10, "queued", None)]], jobs_by_run={10: green_jobs()})
    states = [make_run(10, "in_progress", None), make_run(10, "completed", "success")]

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        api_ok.main_sha = c2
        if states:
            api_ok.ci_pages = [[states.pop(0)]]

    planned = mod.plan(api_ok, c1, tmp_path, 5001, git=git, clock=clock, sleep=sleep_and_advance)
    assert planned["build"] is True
    assert planned["sha"] == c1
    assert sleeps == [30, 30]
    assert len(api_ok.posts) == 1

    # 2. In-progress schedule run does not delay an already-green push run on the same SHA.
    sleeps.clear()
    api_sched = FakeGitHub(
        main_sha=c1,
        ci_pages=[[make_run(20, "in_progress", None, event="schedule"), make_run(10, "completed", "success")]],
        jobs_by_run={10: green_jobs()},
    )
    planned_sched = mod.plan(api_sched, c1, tmp_path, 5002, git=git, clock=clock, sleep=sleeps.append)
    assert planned_sched["build"] is True
    assert sleeps == []

    # 3. In-progress trusted run finishing failure stops immediately and creates zero deployments.
    now[0] = 0.0
    sleeps.clear()
    api_fail = FakeGitHub(main_sha=c1, ci_pages=[[make_run(30, "in_progress", None)]], jobs_by_run={30: green_jobs()})

    def sleep_then_fail(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        api_fail.ci_pages = [[make_run(30, "completed", "failure")]]

    skipped_fail = mod.plan(api_fail, c1, tmp_path, 5003, git=git, clock=clock, sleep=sleep_then_fail)
    assert skipped_fail == {"build": False, "reason": "latest main CI is not green"}
    assert sleeps == [30]
    assert api_fail.posts == []

    # 4. Already-red completed push fails immediately with zero sleep calls.
    now[0] = 0.0
    sleeps.clear()
    api_red = FakeGitHub(main_sha=c1, ci_pages=[[make_run(40, "completed", "failure")]])
    skipped_red = mod.plan(api_red, c1, tmp_path, 5004, git=git, clock=clock, sleep=sleeps.append)
    assert skipped_red == {"build": False, "reason": "latest main CI is not green"}
    assert sleeps == []
    assert api_red.posts == []

    # 5. Still-pending CI at the 45-minute bound clamps the final sleep and skips cleanly.
    now[0] = 0.0
    sleeps.clear()
    api_timeout = FakeGitHub(main_sha=c1, ci_pages=[[make_run(50, "in_progress", None)]])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    assert mod.wait_for_green_main_ci(api_timeout, c1, deadline=45.0, clock=clock, sleep=fake_sleep) is False
    assert sleeps == [30, 15.0]
    assert now[0] == 45.0

    now[0] = 0.0
    sleeps.clear()
    skipped_timeout = mod.plan(api_timeout, c1, tmp_path, 5005, git=git, clock=clock, sleep=fake_sleep)
    assert skipped_timeout == {"build": False, "reason": "latest main CI is not green"}
    assert sum(sleeps) == mod.CI_WAIT_SECONDS
    assert api_timeout.posts == []

    # 6. Admitted SHA turning red during notes collection still skips before creating a deployment.
    api_late_red = FakeGitHub(
        main_sha=c1,
        ci_pages=[[make_run(60, "completed", "success")]],
        jobs_by_run={60: green_jobs()},
    )
    orig_build_notes = mod.build_notes

    def notes_then_red(*args, **kwargs):
        rendered = orig_build_notes(*args, **kwargs)
        api_late_red.ci_pages = [[make_run(61, "completed", "failure", event="workflow_dispatch")]]
        return rendered

    mod.build_notes = notes_then_red
    try:
        skipped_late = mod.plan(api_late_red, c1, tmp_path, 5006, git=git, clock=clock, sleep=fake_sleep)
    finally:
        mod.build_notes = orig_build_notes
    assert skipped_late == {"build": False, "reason": "main CI changed while planning"}
    assert api_late_red.posts == []


def test_daily_test_release_nocheck_only_for_ubuntu_deb_and_fedora_rpm_and_keeps_lint_and_smoke(tmp_path):
    daily = (ROOT / ".github/workflows/daily-test-release.yml").read_text()

    # Daily test release passes --nocheck ONLY for ubuntu:26.04 deb and fedora rpm;
    # debian:testing, openSUSE, and Arch keep build-time tests enabled.
    deb_step = (
        "      - name: Build test .deb\n"
        "        env:\n"
        "          BASE: ${{ matrix.base }}\n"
        "          SKIP_TESTS: ${{ startsWith(matrix.base, 'ubuntu:26.04@sha256:') && '1' || '0' }}\n"
        "        run: |\n"
        '          if [ "$SKIP_TESTS" = 1 ]; then\n'
        '            build-aux/deb/build.sh --nocheck "$BASE"\n'
        "          else\n"
        '            build-aux/deb/build.sh "$BASE"\n'
        "          fi\n"
    )
    rpm_step = (
        "      - name: Build test .rpm\n"
        "        env:\n"
        "          BASE: ${{ matrix.base }}\n"
        "          SKIP_TESTS: ${{ matrix.name == 'fedora' && '1' || '0' }}\n"
        "        run: |\n"
        '          if [ "$SKIP_TESTS" = 1 ]; then\n'
        '            build-aux/rpm/build.sh --nocheck "$BASE"\n'
        "          else\n"
        '            build-aux/rpm/build.sh "$BASE"\n'
        "          fi\n"
    )
    assert daily.count(deb_step) == 1
    assert daily.count(rpm_step) == 1
    assert "\n      - run: build-aux/arch/build.sh\n" in daily
    assert daily.count("--nocheck") == 2

    # CI and stable release workflows never pass --nocheck.
    for wf in ("ci.yml", "deb.yml", "packages.yml", "release.yml", "rpm-canary.yml"):
        assert "nocheck" not in (ROOT / ".github/workflows" / wf).read_text(), wf
    assert "nocheck" not in (ROOT / "build-aux/arch/build.sh").read_text()

    if not shutil.which("sh"):
        return

    # Exercise build-aux/deb/build.sh and build-aux/rpm/build.sh end-to-end with a stubbed
    # container engine to prove:
    # 1) invalid arguments fail closed (exit 2) without invoking the container engine,
    # 2) default mode keeps build-time tests on even if NOCHECK=1 is in the caller environment,
    # 3) --nocheck sets DEB_BUILD_OPTIONS+=nocheck / rpmbuild --nocheck while preserving
    #    lintian/rpmlint, smoke-installed.py, validate_installed.py, and container_offline_smoke.
    repo = tmp_path / "repo"
    (repo / "build-aux/deb").mkdir(parents=True)
    (repo / "build-aux/rpm").mkdir(parents=True)
    (repo / "build-aux/ci").mkdir(parents=True)
    (repo / "debian").mkdir(parents=True)
    (repo / "tests/parakeet").mkdir(parents=True)
    for rel in (
        "build-aux/container.sh",
        "build-aux/ci/retry.sh",
        "build-aux/ci/ci-image.sh",
        "build-aux/deb/build.sh",
        "build-aux/deb/Dockerfile",
        "build-aux/rpm/build.sh",
    ):
        dst = repo / rel
        shutil.copyfile(ROOT / rel, dst)
        dst.chmod((ROOT / rel).stat().st_mode)
    for stub in ("build-aux/whistle-cache.sh", "build-aux/sherpa-cache.sh"):
        p = repo / stub
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    parakeet_cache = tmp_path / "parakeet-cache"
    (
        parakeet_cache
        / "parakeet-ultra-0.6b-int8-v2"
        / "50c15b94ea9cb1c2f8dbcdcbc0f15b450169154deeb206923cb5388f60892366"
    ).mkdir(parents=True)
    (repo / "tests/parakeet/prepare_model.py").write_text("import sys\nsys.exit(0)\n")
    (repo / "meson.build").write_text("project(\n  'cassini-talk',\n  version: '0.4.3~beta.1',\n)\n")
    (repo / "debian/changelog").write_text("cassini-talk (0.4.3~beta.1) unstable; urgency=medium\n")
    (repo / "debian/control").write_text("Source: cassini-talk\n")
    (repo / "build-aux/rpm/cassini-talk.spec").write_text("Name: cassini-talk\nVersion: 0.4.3~beta.1\n")

    log_file = tmp_path / "engine.jsonl"
    fake_engine = tmp_path / "fake-engine"
    fake_engine.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "log_path = pathlib.Path(os.environ['FAKE_ENGINE_LOG'])\n"
        "entry = {'cmd': args[0], 'args': args[1:]}\n"
        "if args[0] == 'run' and 'sh' in args and '-ec' in args:\n"
        "    idx = args.index('-ec')\n"
        "    script = args[idx + 1]\n"
        "    env_map = {}\n"
        "    out_dir = None\n"
        "    for i, tok in enumerate(args):\n"
        "        if tok == '-e' and i + 1 < len(args) and '=' in args[i + 1]:\n"
        "            k, v = args[i + 1].split('=', 1)\n"
        "            env_map[k] = v\n"
        "        elif tok == '-v' and i + 1 < len(args) and ':/out:' in args[i + 1]:\n"
        "            out_dir = pathlib.Path(args[i + 1].split(':', 1)[0])\n"
        "    if 'NOCHECK' in env_map:\n"
        "        entry['kind'] = 'build'\n"
        "        entry['env'] = env_map\n"
        "        entry['has_lintian'] = 'lintian --info --fail-on error \"../$DEB\"' in script\n"
        "        entry['has_rpmlint'] = 'rpmlint -r /in/build-aux/rpm/cassini-talk-rpmlintrc' in script\n"
        "        entry['has_smoke'] = (\n"
        "            'python3 -I /in/build-aux/smoke-installed.py' in script\n"
        "            and 'python3 -I /in/build-aux/ci/validate_installed.py' in script\n"
        "        )\n"
        "        if 'DEB' in env_map:\n"
        "            probe = subprocess.check_output(\n"
        "                ['sh', '-ec', (\n"
        '                    \'[ "$NOCHECK" != 1 ] || export DEB_BUILD_OPTIONS="${DEB_BUILD_OPTIONS:+$DEB_BUILD_OPTIONS }nocheck"; \'\n'
        '                    \'printf "%s" "${DEB_BUILD_OPTIONS:-}"\'\n'
        "                )],\n"
        "                env={'PATH': os.environ['PATH'], 'NOCHECK': env_map['NOCHECK'], 'DEB_BUILD_OPTIONS': env_map.get('DEB_BUILD_OPTIONS', '')},\n"
        "                text=True,\n"
        "            )\n"
        "            entry['deb_build_options'] = probe\n"
        "            if out_dir is not None:\n"
        "                (out_dir / env_map['OUT']).write_bytes(b'deb')\n"
        "        elif 'NAME_FILE' in env_map:\n"
        "            probe = subprocess.check_output(\n"
        "                ['sh', '-ec', (\n"
        "                    'set --; '\n"
        '                    \'[ "$NOCHECK" != 1 ] || set -- "$@" --nocheck; \'\n'
        '                    \'printf "%s" "$*"\'\n'
        "                )],\n"
        "                env={'PATH': os.environ['PATH'], 'NOCHECK': env_map['NOCHECK']},\n"
        "                text=True,\n"
        "            )\n"
        "            entry['rpmbuild_extra_args'] = probe\n"
        "            if out_dir is not None:\n"
        "                rpm_file = f\"cassini-talk-{env_map['VERSION']}-1.fc44.x86_64.rpm\"\n"
        "                (out_dir / rpm_file).write_bytes(b'rpm')\n"
        "                (out_dir / env_map['NAME_FILE']).write_text(rpm_file + '\\n')\n"
        "    elif '--network' in args and args[args.index('--network') + 1] == 'none':\n"
        "        entry['kind'] = 'offline_smoke'\n"
        "        entry['has_offline_smoke'] = 'smoke-installed.py --whistle' in script\n"
        "with log_path.open('a') as fh:\n"
        "    fh.write(json.dumps(entry) + '\\n')\n"
    )
    fake_engine.chmod(0o755)

    base_env = {
        **os.environ,
        "CONTAINER_ENGINE": str(fake_engine),
        "FAKE_ENGINE_LOG": str(log_file),
        "CASSINI_PARAKEET_TEST_CACHE": str(parakeet_cache),
        "NOCHECK": "1",  # Ambient NOCHECK=1 must NEVER turn checks off without --nocheck!
    }

    # Invalid CLI invocations must exit 2 before calling CONTAINER_ENGINE.
    for script in ("build-aux/deb/build.sh", "build-aux/rpm/build.sh"):
        for bad_args in (["--unknown"], [""], ["ubuntu:26.04", "--nocheck"], ["--nocheck", ""]):
            res = subprocess.run([str(repo / script), *bad_args], cwd=repo, env=base_env, capture_output=True)
            assert res.returncode == 2, (script, bad_args)
    assert not log_file.exists()

    def run_build(script: str, cli_args: list[str]) -> list[dict]:
        log_file.unlink(missing_ok=True)
        subprocess.run([str(repo / script), *cli_args], cwd=repo, env=base_env, check=True)
        return [json.loads(line) for line in log_file.read_text().splitlines()]

    # Default deb build (e.g. debian:testing): tests stay ON despite ambient NOCHECK=1.
    deb_default = run_build("build-aux/deb/build.sh", ["debian:testing@sha256:" + "2" * 64])
    deb_run = next(e for e in deb_default if e.get("kind") == "build")
    assert deb_run["env"]["NOCHECK"] == "0"
    assert deb_run["deb_build_options"] == ""
    assert deb_run["has_lintian"] is True and deb_run["has_smoke"] is True
    assert any(e.get("kind") == "offline_smoke" and e["has_offline_smoke"] for e in deb_default)

    # --nocheck deb build (ubuntu:26.04 daily test release): nocheck enabled, lintian + smoke + offline smoke preserved.
    deb_nocheck = run_build("build-aux/deb/build.sh", ["--nocheck", "ubuntu:26.04@sha256:" + "0" * 64])
    deb_nc_run = next(e for e in deb_nocheck if e.get("kind") == "build")
    assert deb_nc_run["env"]["NOCHECK"] == "1"
    assert deb_nc_run["deb_build_options"] == "nocheck"
    assert deb_nc_run["has_lintian"] is True and deb_nc_run["has_smoke"] is True
    assert any(e.get("kind") == "offline_smoke" and e["has_offline_smoke"] for e in deb_nocheck)

    # Default rpm build (e.g. openSUSE): tests stay ON despite ambient NOCHECK=1.
    rpm_default = run_build(
        "build-aux/rpm/build.sh", ["registry.opensuse.org/opensuse/tumbleweed:latest@sha256:" + "3" * 64]
    )
    rpm_run = next(e for e in rpm_default if e.get("kind") == "build")
    assert rpm_run["env"]["NOCHECK"] == "0"
    assert rpm_run["rpmbuild_extra_args"] == ""
    assert rpm_run["has_rpmlint"] is True and rpm_run["has_smoke"] is True
    assert any(e.get("kind") == "offline_smoke" and e["has_offline_smoke"] for e in rpm_default)

    # --nocheck rpm build (fedora:44 daily test release): --nocheck passed to rpmbuild, rpmlint + smoke + offline smoke preserved.
    rpm_nocheck = run_build("build-aux/rpm/build.sh", ["--nocheck", "fedora:44@sha256:" + "1" * 64])
    rpm_nc_run = next(e for e in rpm_nocheck if e.get("kind") == "build")
    assert rpm_nc_run["env"]["NOCHECK"] == "1"
    assert rpm_nc_run["rpmbuild_extra_args"] == "--nocheck"
    assert rpm_nc_run["has_rpmlint"] is True and rpm_nc_run["has_smoke"] is True
    assert any(e.get("kind") == "offline_smoke" and e["has_offline_smoke"] for e in rpm_nocheck)
