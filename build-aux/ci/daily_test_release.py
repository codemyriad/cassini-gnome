#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan, version-bump, publish and record unattended GNOME daily test releases."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# -I omits the script directory; import only this checkout's trusted CI helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import green_main  # noqa: E402
import release_metadata  # noqa: E402
import release_sums  # noqa: E402

ENVIRONMENT = "gnome-daily-test-release"
REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
STABLE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
STABLE_TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")
TEST_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)-beta\.([1-9]\d*)")
TEST_TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)-beta\.([1-9]\d*)")
# Scheduled nightly ci.yml runs probe unpinned rolling base images outside merge
# and release gates; daily test releases only admit pinned push / dispatch runs.
TRUSTED_CI_EVENTS = {"push", "workflow_dispatch"}
MAX_PAGES = 50
MAX_NOTES_BYTES = 60_000


class GitRepo:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, timeout=60).strip()

    def is_ancestor(self, older: str, newer: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.root), "merge-base", "--is-ancestor", older, newer],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).returncode
            == 0
        )

    def commit_exists(self, rev: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        sha = result.stdout.strip()
        return sha if result.returncode == 0 and SHA_RE.fullmatch(sha) else None


class GitHub:
    def __init__(self, repo: str):
        if not REPO_RE.fullmatch(repo):
            raise ValueError(f"Invalid repository: {repo}")
        self.repo = repo

    def request(self, path: str, *, method: str = "GET", data: dict | None = None, allow_404: bool = False):
        args = ["gh", "api", "--method", method, f"repos/{self.repo}/{path}"]
        payload = None
        if data is not None:
            args += ["--input", "-"]
            payload = json.dumps(data)
        try:
            result = subprocess.run(
                args,
                input=payload,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            combined = f"{error.stdout or ''}\n{error.stderr or ''}"
            if allow_404 and ("HTTP 404" in combined or '"status": "404"' in combined or '"status":"404"' in combined):
                return None
            raise
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def pages(self, path: str, key: str | None = None):
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            response = self.request(f"{path}{separator}per_page=100&page={page}")
            rows = response[key] if key else response
            yield from rows
            if len(rows) < 100:
                return
        raise ValueError(f"Pagination limit exceeded for {path}")


def green_main_ci(api: GitHub, sha: str) -> bool:
    eligible = [
        run
        for run in api.pages(f"actions/workflows/ci.yml/runs?branch=main&head_sha={sha}", "workflow_runs")
        if (run.get("head_repository") or {}).get("full_name") == api.repo
        and run.get("event") in TRUSTED_CI_EVENTS
        and run.get("head_branch") == "main"
        and run.get("head_sha") == sha
    ]
    if not eligible:
        return False
    latest = max(eligible, key=lambda run: run["id"])
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        return False
    jobs = list(api.pages(f"actions/runs/{latest['id']}/jobs?filter=latest", "jobs"))
    try:
        green_main.validate_test_release_jobs(jobs, latest.get("html_url", ""))
    except ValueError:
        return False
    return True


def release_published(api: GitHub, tag: str) -> bool:
    remote = api.request(f"releases/tags/{tag}", allow_404=True)
    if not remote or remote.get("draft") or not remote.get("prerelease"):
        return False
    uploaded = [asset for asset in remote.get("assets") or [] if asset.get("state", "uploaded") == "uploaded"]
    return len(uploaded) == 6


def source_meson_version(text: str) -> str:
    match = re.search(r"^\s*version:\s*'([^']+)'", text, re.MULTILINE)
    if match is None or not STABLE_VERSION_RE.fullmatch(match[1]):
        raise ValueError("Unsupported meson.build version")
    return match[1]


def beta_base(meson_version: str, released_bases: set[str]) -> str:
    match = STABLE_VERSION_RE.fullmatch(meson_version)
    if match is None:
        raise ValueError(f"Unsupported base version: {meson_version}")
    major, minor, patch = map(int, match.groups())
    base = f"{major}.{minor}.{patch}"
    while base in released_bases:
        patch += 1
        base = f"{major}.{minor}.{patch}"
    return base


def next_version(
    meson_version: str, released_bases: set[str], reserved_pairs: set[tuple[str, int]]
) -> dict[str, str | int]:
    base = beta_base(meson_version, released_bases)
    beta = max([0, *(num for prior_base, num in reserved_pairs if prior_base == base)]) + 1
    return {
        "base_version": base,
        "beta": beta,
        "version_name": f"{base}-beta.{beta}",
        "package_version": f"{base}~beta.{beta}",
        "arch_pkgver": f"{base}beta{beta}",
    }


def literal_code_span(title: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", title)
    clean = " ".join(clean.split()) or "(untitled)"
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", clean)), default=0)
    fence = "`" * (longest + 1)
    if clean[0] in ("`", " ") or clean[-1] in ("`", " "):
        return f"{fence} {clean} {fence}"
    return f"{fence}{clean}{fence}"


def collect_history(api: GitHub, sha: str, git: GitRepo) -> tuple[set[str], set[tuple[str, int]], str | None]:
    released_bases: set[str] = set()
    reserved_pairs: set[tuple[str, int]] = set()
    local_tags = git.run("tag", "--list", "v*").splitlines()
    stable_tags: set[str] = set()
    for tag in local_tags:
        if match := STABLE_TAG_RE.fullmatch(tag):
            released_bases.add(match[1])
            stable_tags.add(tag)
        elif match := TEST_TAG_RE.fullmatch(tag):
            reserved_pairs.add((match[1], int(match[2])))

    releases = list(api.pages("releases"))
    nightly_candidates: list[tuple[str, int, dict]] = []
    published_nightly_tags: set[str] = set()
    for rel in releases:
        tag = rel.get("tag_name") or ""
        if not rel.get("draft") and not rel.get("prerelease") and (match := STABLE_TAG_RE.fullmatch(tag)):
            released_bases.add(match[1])
            stable_tags.add(tag)
        if match := TEST_TAG_RE.fullmatch(tag):
            reserved_pairs.add((match[1], int(match[2])))
            uploaded = [a for a in rel.get("assets") or [] if a.get("state", "uploaded") == "uploaded"]
            if not rel.get("draft") and rel.get("prerelease") and len(uploaded) == 6:
                published_nightly_tags.add(tag)
            if not rel.get("draft") and rel.get("prerelease") and (rel.get("name") or "").startswith("nightly"):
                nightly_candidates.append((rel.get("published_at") or "", int(rel.get("id") or 0), rel))

    baseline: str | None = None

    def fold_candidate(candidate_sha: str | None) -> None:
        nonlocal baseline
        if not candidate_sha:
            return
        common = sha if candidate_sha == sha else git.run("merge-base", candidate_sha, sha)
        if baseline is None or common == sha or git.is_ancestor(baseline, common):
            baseline = common

    if nightly_candidates:
        _, _, newest_nightly = max(nightly_candidates, key=lambda item: (item[0], item[1]))
        target = newest_nightly.get("target_commitish") or ""
        nightly_sha = git.commit_exists(target) if SHA_RE.fullmatch(target) else None
        if nightly_sha is None:
            nightly_sha = git.commit_exists(newest_nightly["tag_name"])
        fold_candidate(nightly_sha)

    found_deployment_baseline = False
    for deployment in api.pages(f"deployments?environment={ENVIRONMENT}"):
        payload = deployment.get("payload")
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise ValueError("Invalid test-release deployment payload")
        version_name = payload.get("version_name")
        match = TEST_VERSION_RE.fullmatch(version_name) if isinstance(version_name, str) else None
        if match is None or type(payload.get("run_id")) is not int:
            raise ValueError("Invalid test-release reservation")
        reserved_pairs.add((match[1], int(match[2])))
        if not found_deployment_baseline:
            states = {status.get("state") for status in api.pages(f"deployments/{deployment['id']}/statuses")}
            succeeded = "success" in states
            if not states.intersection({"success", "failure"}):
                tag = f"v{version_name}"
                succeeded = tag in published_nightly_tags or release_published(api, tag)
            if succeeded:
                found_deployment_baseline = True
                dep_sha = deployment.get("sha") or ""
                fold_candidate(git.commit_exists(dep_sha) if SHA_RE.fullmatch(dep_sha) else None)

    ordered_stable = sorted(stable_tags, key=lambda t: tuple(map(int, t[1:].split("."))), reverse=True)
    for tag in ordered_stable[:5]:
        fold_candidate(git.commit_exists(tag))

    if baseline is not None and baseline != sha and not git.is_ancestor(baseline, sha):
        baseline = git.run("merge-base", baseline, sha)
    return released_bases, reserved_pairs, baseline


def build_notes(api: GitHub, baseline: str | None, sha: str, git: GitRepo) -> str:
    if baseline is not None:
        if baseline != sha and not git.is_ancestor(baseline, sha):
            baseline = git.run("merge-base", baseline, sha)
        commits = (
            []
            if baseline == sha
            else git.run("log", "--first-parent", "--reverse", "--format=%H", f"{baseline}..{sha}").splitlines()
        )
    else:
        commits = list(reversed(git.run("log", "--first-parent", "-n", "50", "--format=%H", sha).splitlines()))

    commit_set = set(commits)
    merged: dict[int, str] = {}
    for commit in commits:
        for pull in api.pages(f"commits/{commit}/pulls"):
            number = pull.get("number")
            if (
                pull.get("merged_at")
                and (pull.get("base") or {}).get("ref") == "main"
                and pull.get("merge_commit_sha") in commit_set
                and isinstance(number, int)
                and number not in merged
            ):
                merged[number] = literal_code_span(str(pull.get("title") or ""))

    header = f"Source: `{sha}`\n\n"
    bullets = [f"- {title} (#{number})" for number, title in merged.items()]
    if not bullets:
        bullets = ["- Changes on main since the previous test release."]

    kept: list[str] = []
    suffix = "- ...\n"
    budget = MAX_NOTES_BYTES - len(header.encode("utf-8"))
    used = 0
    for index, line in enumerate(bullets):
        encoded = (line + "\n").encode("utf-8")
        reserve = len(suffix.encode("utf-8")) if index + 1 < len(bullets) else 0
        if used + len(encoded) + reserve > budget:
            kept.append("- ...")
            break
        kept.append(line)
        used += len(encoded)
    return header + "\n".join(kept) + "\n"


def plan(api: GitHub, sha: str, root: Path, run_id: int, *, git: GitRepo | None = None) -> dict:
    git_repo = git or GitRepo(root)
    if not SHA_RE.fullmatch(sha):
        raise ValueError("Invalid main commit SHA")
    if api.request("commits/main").get("sha") != sha:
        return {"build": False, "reason": "main moved before planning"}
    if not green_main_ci(api, sha):
        return {"build": False, "reason": "latest main CI is not green"}

    meson_version = source_meson_version((root / "meson.build").read_text())
    released_bases, reserved_pairs, baseline = collect_history(api, sha, git_repo)
    if baseline == sha:
        return {"build": False, "reason": "main is unchanged since the last test release"}

    version_info = next_version(meson_version, released_bases, reserved_pairs)
    notes = build_notes(api, baseline, sha, git_repo)

    if api.request("commits/main").get("sha") != sha or not green_main_ci(api, sha):
        return {"build": False, "reason": "main or its CI changed while planning"}

    payload = {
        "schema": 1,
        "version_name": version_info["version_name"],
        "package_version": version_info["package_version"],
        "arch_pkgver": version_info["arch_pkgver"],
        "base_version": version_info["base_version"],
        "beta": version_info["beta"],
        "run_id": run_id,
    }
    deployment = api.request(
        "deployments",
        method="POST",
        data={
            "ref": sha,
            "environment": ENVIRONMENT,
            "auto_merge": False,
            "required_contexts": [],
            "transient_environment": True,
            "production_environment": False,
            "payload": payload,
        },
    )
    return {
        "build": True,
        "sha": sha,
        "baseline": baseline,
        "deployment_id": deployment["id"],
        **payload,
        "notes": notes,
    }


def plan_metadata(value: dict) -> dict:
    meta = {key: item for key, item in value.items() if key != "notes"}
    if "notes" in value:
        meta["notes_sha256"] = hashlib.sha256(value["notes"].encode("utf-8")).hexdigest()
    return meta


def load_plan(metadata: dict, plan_dir: Path) -> dict:
    value = json.loads((plan_dir / "test-release-plan.json").read_text())
    if plan_metadata(value) != metadata:
        raise ValueError("Release plan artifact does not match its reserved metadata")
    return value


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    original = path.read_text()
    updated, count = re.subn(pattern, lambda _m: replacement, original, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected one version match in {path}")
    path.write_text(updated)


def apply_plan(value: dict, sha: str, root: Path, *, git: GitRepo | None = None) -> None:
    git_repo = git or GitRepo(root)
    if value.get("build") is not True or value.get("sha") != sha or not SHA_RE.fullmatch(sha):
        raise ValueError("Test build plan is not bound to this main commit")
    if git_repo.run("rev-parse", "HEAD") != sha:
        raise ValueError("Checkout HEAD does not match the admitted main commit")
    if git_repo.run("status", "--porcelain"):
        raise ValueError("Checkout must be clean before applying test version")

    version_name = value.get("version_name")
    match = TEST_VERSION_RE.fullmatch(version_name) if isinstance(version_name, str) else None
    if match is None:
        raise ValueError(f"Invalid test version: {version_name}")
    base, beta = match[1], int(match[2])
    expected_pkg = f"{base}~beta.{beta}"
    expected_arch = f"{base}beta{beta}"
    if value.get("package_version") != expected_pkg or value.get("arch_pkgver") != expected_arch:
        raise ValueError("Plan package versions disagree with version_name")
    notes = value.get("notes")
    if not isinstance(notes, str) or not notes.strip() or len(notes.encode("utf-8")) > MAX_NOTES_BYTES:
        raise ValueError("Invalid release notes in plan")

    timestamp = int(git_repo.run("show", "-s", "--format=%ct", sha))
    committed_at = datetime.fromtimestamp(timestamp, UTC)
    iso_date = committed_at.strftime("%Y-%m-%d")
    man_month = committed_at.strftime("%B %Y")
    rfc2822_date = email.utils.format_datetime(committed_at)

    replace_once(root / "meson.build", r"^(\s*version:\s*)'[^']+'", f"          version: '{expected_pkg}'")
    changelog = root / "debian/changelog"
    stanza = (
        f"cassini-talk ({expected_pkg}) unstable; urgency=medium\n\n"
        f"  * Unattended daily test build from main ({sha[:12]}).\n\n"
        f" -- Codemyriad <cassini@codemyriad.io>  {rfc2822_date}\n\n"
    )
    changelog.write_text(stanza + changelog.read_text())
    replace_once(root / "build-aux/rpm/cassini-talk.spec", r"^Version:\s*\S+", f"Version:        {expected_pkg}")
    replace_once(root / "build-aux/arch/PKGBUILD", r"^pkgver=\S+", f"pkgver={expected_arch}")
    replace_once(
        root / "data/cassini-talk.1",
        r'\A\.TH CASSINI-TALK 1 "[^"]+" "cassini-talk [^"]+"',
        f'.TH CASSINI-TALK 1 "{man_month}" "cassini-talk {expected_pkg}"',
    )
    replace_once(
        root / "data/io.codemyriad.CassiniTalk.metainfo.xml.in",
        r"^(\s*<releases>)$",
        f'  <releases>\n    <release version="{expected_pkg}" type="development" date="{iso_date}"/>',
    )
    release_metadata.check_prerelease(root, version_name)


def verify_staged_versions(staged: list[Path], value: dict) -> None:
    match = TEST_VERSION_RE.fullmatch(str(value.get("version_name") or ""))
    if match is None:
        raise ValueError("Invalid version_name")
    base, beta = match[1], match[2]
    deb_prefix = f"cassini-talk_{base}.beta.{beta}."
    rpm_prefix = f"cassini-talk-{base}.beta.{beta}-1."
    arch_exact = f"cassini-talk-{base}beta{beta}-1-x86_64.pkg.tar.zst"
    names = [path.name for path in staged]
    debs = [name for name in names if name.endswith("_amd64.deb")]
    rpms = [name for name in names if name.endswith(".x86_64.rpm")]
    archs = [name for name in names if name.endswith(".pkg.tar.zst")]
    if len(debs) != 2 or not all(name.startswith(deb_prefix) for name in debs):
        raise ValueError(f"Debian package names do not match {deb_prefix}*: {debs}")
    if len(rpms) != 2 or not all(name.startswith(rpm_prefix) for name in rpms):
        raise ValueError(f"RPM package names do not match {rpm_prefix}*: {rpms}")
    if archs != [arch_exact]:
        raise ValueError(f"Arch package name does not match {arch_exact}: {archs}")


def verify_downloaded_sums(directory: Path) -> None:
    sums_file = directory / "SHA256SUMS"
    lines = sums_file.read_text().splitlines()
    if len(lines) != 5:
        raise ValueError(f"Expected 5 SHA256SUMS lines, found {len(lines)}")
    expected_names: set[str] = set()
    for line in lines:
        digest, sep, name = line.partition("  ")
        if not sep or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Malformed SHA256SUMS line: {line}")
        target = directory / name
        if not target.is_file() or target.is_symlink() or release_sums.sha256(target) != digest:
            raise ValueError(f"Published asset failed SHA-256 verification: {name}")
        expected_names.add(name)
    actual_names = {path.name for path in directory.iterdir() if path.name != "SHA256SUMS"}
    if actual_names != expected_names:
        raise ValueError(f"Published asset set mismatch: {sorted(actual_names)} != {sorted(expected_names)}")
    if shutil.which("sha256sum"):
        subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=directory, check=True, timeout=60)


def publish_release(
    api: GitHub,
    metadata: dict,
    plan_dir: Path,
    artifacts_dir: Path,
    work_dir: Path,
    sha: str,
    *,
    run=subprocess.run,
) -> None:
    value = load_plan(metadata, plan_dir)
    if value.get("build") is not True or value.get("sha") != sha or not SHA_RE.fullmatch(sha):
        raise ValueError("Test release plan is not bound to this main commit")
    version_name = value["version_name"]
    tag = f"v{version_name}"
    if not TEST_TAG_RE.fullmatch(tag):
        raise ValueError(f"Only immutable beta pre-release tags may be published: {tag}")

    staged_dir = work_dir / "staged"
    sums_path = work_dir / "SHA256SUMS"
    staged = release_sums.stage(release_sums.packages(artifacts_dir), staged_dir)
    verify_staged_versions(staged, value)
    release_sums.write_sums(staged, sums_path)
    assets = [*staged, sums_path]
    if len(assets) != 6:
        raise ValueError(f"Expected 6 release assets, got {len(assets)}")

    notes_path = work_dir / "notes.md"
    notes_path.write_text(value["notes"])

    run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            api.repo,
            "--target",
            sha,
            "--title",
            f"nightly — {version_name}",
            "--notes-file",
            str(notes_path),
            "--draft",
            "--prerelease",
            "--latest=false",
            *(str(path) for path in assets),
        ],
        check=True,
        timeout=600,
    )

    draft = next((rel for rel in api.pages("releases") if rel.get("tag_name") == tag), None)
    expected_sizes = {path.name: path.stat().st_size for path in assets}
    remote_sizes = {
        asset["name"]: asset["size"]
        for asset in (draft or {}).get("assets", [])
        if asset.get("state", "uploaded") == "uploaded"
    }
    if (
        draft is None
        or not draft.get("draft")
        or not draft.get("prerelease")
        or draft.get("target_commitish") != sha
        or remote_sizes != expected_sizes
    ):
        raise ValueError("Draft pre-release assets or metadata are incomplete")

    run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            api.repo,
            "--draft=false",
            "--prerelease",
            "--latest=false",
        ],
        check=True,
        timeout=120,
    )

    published_dir = work_dir / "published"
    published_dir.mkdir(parents=True, exist_ok=True)
    run(
        ["gh", "release", "download", tag, "--repo", api.repo, "--dir", str(published_dir)],
        check=True,
        timeout=600,
    )
    verify_downloaded_sums(published_dir)


def finish(api: GitHub, metadata: dict, results: dict[str, str], run_url: str) -> tuple[bool, str]:
    version_name = metadata["version_name"]
    tag = f"v{version_name}"
    published = release_published(api, tag)
    api.request(
        f"deployments/{metadata['deployment_id']}/statuses",
        method="POST",
        data={
            "state": "success" if published else "failure",
            "log_url": run_url,
            "auto_inactive": False,
        },
    )
    if published and results.get("publish") == "success":
        return True, f"published: {tag} ({metadata['sha'][:7]})"
    if published:
        return False, f"failed: publish job ended {results.get('publish')} after {tag} went live ({run_url})"
    failed_jobs = [name for name in ("deb", "rpm", "arch", "publish") if results.get(name) not in ("success", None)]
    cause = ", ".join(f"{name}={results.get(name)}" for name in failed_jobs) or "package build or upload failed"
    return False, f"failed: {cause} ({run_url})"


def report(message: str, *, failed: bool = False) -> None:
    single = " ".join(message.split())
    line = f"[GNOME] daily test release {single}"
    escaped = line.replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
    print(("::error::" if failed else "::notice::") + escaped, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def require_trusted_env() -> tuple[str, str]:
    if os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_EVENT_NAME") not in {
        "schedule",
        "workflow_dispatch",
    }:
        raise ValueError("Daily test releases run only from main on schedule or workflow_dispatch")
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    sha = os.environ.get("GITHUB_SHA") or ""
    if not REPO_RE.fullmatch(repo) or not SHA_RE.fullmatch(sha):
        raise ValueError("Invalid GITHUB_REPOSITORY or GITHUB_SHA")
    return repo, sha


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply", "publish", "finish"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--plan-dir", type=Path, default=Path("."))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--work-dir", type=Path, default=Path("work"))
    args = parser.parse_args()

    repo, sha = require_trusted_env()
    root = args.root.resolve()

    if args.mode == "plan":
        value = plan(GitHub(repo), sha, root, int(os.environ["GITHUB_RUN_ID"]))
        meta_json = json.dumps(plan_metadata(value), separators=(",", ":"))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
            handle.write(f"build={str(value['build']).lower()}\n")
            handle.write(f"sha={sha}\n")
            handle.write(f"plan={meta_json}\n")
        (args.plan_dir / "test-release-plan.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        if value["build"]:
            report(f"planned: v{value['version_name']} ({sha[:7]})")
        else:
            report(f"skipped: {value['reason']}")
        return

    metadata = json.loads(os.environ["DAILY_TEST_PLAN"])
    if args.mode == "apply":
        loaded = load_plan(metadata, args.plan_dir)
        apply_plan(loaded, sha, root)
        return

    if args.mode == "publish":
        args.work_dir.mkdir(parents=True, exist_ok=True)
        publish_release(GitHub(repo), metadata, args.plan_dir, args.artifacts, args.work_dir, sha)
        return

    results = {
        "deb": os.environ.get("DEB_RESULT", ""),
        "rpm": os.environ.get("RPM_RESULT", ""),
        "arch": os.environ.get("ARCH_RESULT", ""),
        "publish": os.environ.get("PUBLISH_RESULT", ""),
    }
    ok, message = finish(GitHub(repo), metadata, results, os.environ["RUN_URL"])
    report(message, failed=not ok)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        detail = (exc.stderr or str(exc)) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        first_line = (detail.strip().splitlines() or [type(exc).__name__])[0][:300]
        report(f"failed: {type(exc).__name__}: {first_line}", failed=True)
        raise SystemExit(1) from exc
