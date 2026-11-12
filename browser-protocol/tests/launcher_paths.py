# SPDX-License-Identifier: GPL-3.0-or-later
"""Classify whether a PR diff requires launcher-macos unit tests and/or the D68.1 capture clock check."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

LAUNCHER_FILES = frozenset(
    {
        'conformance/check-session-events.py',
        'conformance/gaps/gaps.py',
        'conformance/multistream/corpus.json',
        'conformance/pcm.py',
        'schemas/session.schema.json',
        'scenarios/control-relay.mjs',
        'scenarios/folder-clock.mjs',
        'scenarios/live-folder.mjs',
        'scenarios/native-launcher.mjs',
        'scenarios/product-topologies.mjs',
        'scenarios/session-clock.mjs',
        'tests/control-relay.test.mjs',
        'tests/launcher_paths.py',
        'tests/live-folder-driver.test.mjs',
    }
)

CAPTURE_FILES = frozenset(
    {
        '.npmrc',
        'binding.gyp',
        'extension/capture.js',
        'extension/pcm-worklet.js',
        'npm-shrinkwrap.json',
        'package-lock.json',
        'tests/capture-real-time.mjs',
        'tests/launcher_paths.py',
    }
)

LAUNCHER_PACKAGE_KEYS = ('type', 'engines', 'imports', 'dependencies', 'optionalDependencies')


def split_workflow(text: str) -> tuple[str, dict[str, str]] | None:
    if '\njobs:\n' not in text:
        return None
    header, body = text.split('\njobs:\n', 1)
    parsed: dict[str, str] = {}
    for block in re.split(r'\n(?=  [A-Za-z0-9_-]+:\n)', '\n' + body):
        if match := re.match(r'\s*([A-Za-z0-9_-]+):\n', block):
            parsed[match.group(1)] = block.strip('\n')
    return header.strip('\n'), parsed


def workflow_gate_or_macos_changed(base_text: str | None, head_text: str | None) -> bool:
    if base_text is None or head_text is None:
        return True
    base_parts = split_workflow(base_text)
    head_parts = split_workflow(head_text)
    if base_parts is None or head_parts is None:
        return True
    base_header, base_jobs = base_parts
    head_header, head_jobs = head_parts
    if base_header != head_header:
        return True
    for job_name in ('launcher-paths', 'launcher-macos'):
        if job_name not in base_jobs or job_name not in head_jobs:
            return True
        if base_jobs[job_name] != head_jobs[job_name]:
            return True
    return False


def _is_ordinary_test_script(name: str) -> bool:
    """Only `test` and `test:*` scripts are exempt; npm lifecycle hooks (preinstall, postinstall, prepare, etc.) run during `npm ci`."""
    return name == 'test' or name.startswith('test:')


def _non_test_scripts(pkg: dict[str, object]) -> object:
    if 'scripts' not in pkg:
        return {}
    scripts = pkg['scripts']
    if not isinstance(scripts, dict):
        return ('__invalid_scripts__', scripts)
    return {name: cmd for name, cmd in scripts.items() if not _is_ordinary_test_script(name)}


def package_json_effects(base_text: str | None, head_text: str | None) -> tuple[bool, bool]:
    """Return (launcher_needed, capture_needed) for a package.json change."""
    if base_text is None or head_text is None:
        return True, True
    try:
        base_pkg = json.loads(base_text)
        head_pkg = json.loads(head_text)
    except json.JSONDecodeError:
        return True, True
    if not isinstance(base_pkg, dict) or not isinstance(head_pkg, dict):
        return True, True

    base_non_scripts = {k: v for k, v in base_pkg.items() if k != 'scripts'}
    head_non_scripts = {k: v for k, v in head_pkg.items() if k != 'scripts'}
    lifecycle_scripts_changed = _non_test_scripts(base_pkg) != _non_test_scripts(head_pkg)
    capture_needed = (base_non_scripts != head_non_scripts) or lifecycle_scripts_changed

    base_launcher = {k: base_pkg.get(k) for k in LAUNCHER_PACKAGE_KEYS if k in base_pkg}
    head_launcher = {k: head_pkg.get(k) for k in LAUNCHER_PACKAGE_KEYS if k in head_pkg}
    launcher_needed = base_launcher != head_launcher
    return launcher_needed, capture_needed


def classify_changes(
    changed_files: list[str],
    read_rev_file=lambda _rev, _path: None,
    base_rev: str = 'BASE',
    head_rev: str = 'HEAD',
) -> dict[str, bool]:
    launcher = False
    capture = False
    for path in changed_files:
        if path in LAUNCHER_FILES:
            launcher = True
        if path.startswith('conformance/live-folder/') and not path.endswith('.md'):
            launcher = True
        if path in CAPTURE_FILES or path.endswith('/.npmrc'):
            capture = True
        if path == 'package.json':
            pkg_launcher, pkg_capture = package_json_effects(
                read_rev_file(base_rev, path),
                read_rev_file(head_rev, path),
            )
            launcher = launcher or pkg_launcher
            capture = capture or pkg_capture
        if path == '.github/workflows/ci.yml' and workflow_gate_or_macos_changed(
            read_rev_file(base_rev, path),
            read_rev_file(head_rev, path),
        ):
            launcher = True
            capture = True
    return {'launcher': launcher, 'capture': capture, 'changed': launcher or capture}


def git_ok(repo: pathlib.Path, *args: str) -> bool:
    return (
        subprocess.run(
            ['git', '-C', str(repo), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def git_stdout(repo: pathlib.Path, *args: str) -> str | None:
    proc = subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def resolve_compare_base(repo: pathlib.Path, base_sha: str, head: str = 'HEAD') -> str | None:
    if git_ok(repo, 'rev-parse', '--verify', '--quiet', f'{head}^2^{{commit}}'):
        parent = git_stdout(repo, 'rev-parse', '--verify', f'{head}^1^{{commit}}')
        if parent:
            return parent.strip()
    if not base_sha or set(base_sha) == {'0'} or not git_ok(repo, 'cat-file', '-e', f'{base_sha}^{{commit}}'):
        return None
    merged = git_stdout(repo, 'merge-base', base_sha, head)
    if merged:
        return merged.strip()
    return None


def evaluate_git(
    repo: pathlib.Path,
    *,
    event_name: str,
    base_sha: str,
    head: str = 'HEAD',
    perf_gate: bool = False,
) -> tuple[dict[str, bool], str | None]:
    # Pushes to main, nightly/manual runs, and full-ci PRs always run full macOS coverage.
    if event_name != 'pull_request' or perf_gate:
        return {'launcher': True, 'capture': True, 'changed': True}, None
    compare_base = resolve_compare_base(repo, base_sha, head)
    if compare_base is None:
        return {'launcher': True, 'capture': True, 'changed': True}, None
    diff_out = git_stdout(repo, 'diff', '--no-renames', '--name-only', '-z', compare_base, head)
    if diff_out is None:
        return {'launcher': True, 'capture': True, 'changed': True}, None
    changed_files = [entry for entry in diff_out.split('\0') if entry]
    result = classify_changes(
        changed_files,
        read_rev_file=lambda rev, rel: git_stdout(repo, 'show', f'{rev}:{rel}'),
        base_rev=compare_base,
        head_rev=head,
    )
    return result, compare_base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=pathlib.Path, default=ROOT)
    parser.add_argument('--event-name', default=os.environ.get('EVENT_NAME', ''))
    parser.add_argument('--base', default=os.environ.get('BASE', ''))
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--perf-gate', default=os.environ.get('CASSINI_PERF_GATE', ''))
    parser.add_argument('--github-output', type=pathlib.Path)
    args = parser.parse_args(argv)

    result, compare_base = evaluate_git(
        args.repo,
        event_name=args.event_name,
        base_sha=args.base,
        head=args.head,
        perf_gate=(args.perf_gate == '1'),
    )
    lines = [f"{key}={'true' if value else 'false'}" for key, value in result.items()]
    if args.github_output is not None:
        args.github_output.parent.mkdir(parents=True, exist_ok=True)
        with args.github_output.open('a', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')
    base_label = f'event:{args.event_name or "non-pr"}' if args.event_name != 'pull_request' else (compare_base or 'unknown')
    print(
        f"launcher-macos: changed={str(result['changed']).lower()} "
        f"(launcher={str(result['launcher']).lower()}, capture={str(result['capture']).lower()}, "
        f'base={base_label})'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
