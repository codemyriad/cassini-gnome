# SPDX-License-Identifier: GPL-3.0-or-later
"""D66: one CI run per PR push, superseded PR runs cancelled, main never cancelled, every job bounded."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github/workflows'


def _load_launcher_paths():
    spec = importlib.util.spec_from_file_location('launcher_paths', ROOT / 'tests/launcher_paths.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jobs(workflow):
    body = workflow.read_text().split('\njobs:\n', 1)[1]
    for block in re.split(r'\n(?=  [A-Za-z0-9_-]+:\n)', '\n' + body):
        if match := re.match(r'\s*([A-Za-z0-9_-]+):\n', block):
            yield match.group(1), block


# CM_RUNNER moves a job to the cm-hetzner runners only when it is exactly "hetzner" and the event
# is in the explicit trusted allow-list (push, schedule, workflow_dispatch, or a same-repo
# pull_request). The labels are a constant and the variable is never parsed, so any other value,
# any fork PR (including deleted forks), or any unallowlisted event keeps ubuntu-24.04.
CM_HETZNER = ['self-hosted', 'cm-hetzner']
EVENT_ALLOWLIST = (
    r"\(github\.event_name == 'push' \|\| github\.event_name == 'schedule' \|\| "
    r"github\.event_name == 'workflow_dispatch' \|\| "
    r"\(github\.event_name == 'pull_request' && "
    r"github\.event\.pull_request\.head\.repo\.full_name == github\.repository\)\)"
)
RUNNER_SELECTOR = re.compile(
    rf"^\$\{{\{{ {EVENT_ALLOWLIST} && vars\.CM_RUNNER == 'hetzner' && "
    r"fromJSON\('(?P<labels>\[\"self-hosted\",\"cm-hetzner\"\])'\) \|\| '(?P<default>ubuntu-24\.04)' \}\}$")


class _CIString(str):
    """GitHub Actions string comparison (`==` / `!=`) is case-insensitive."""

    def __eq__(self, other):
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __ne__(self, other):
        return not self.__eq__(other)


class _NullContext:
    """GitHub Actions dereferences missing/null context properties to null instead of raising."""

    def __getattr__(self, _name):
        return self

    def __bool__(self):
        return False


_NULL = _NullContext()


def runs_on_values():
    """(workflow, runs-on) for every job; a block runs-on (a YAML list or mapping) is joined into one string."""
    for workflow in sorted(WORKFLOWS.glob('*.yml')):
        lines = workflow.read_text().splitlines()
        for i, line in enumerate(lines):
            if match := re.match(r'^    runs-on:(.*)$', line):
                value = [match.group(1).strip()]
                for following in lines[i + 1:]:
                    if not following.startswith('      '):
                        break
                    value.append(following.strip())
                yield workflow.name, '\n'.join(v for v in value if v)


def select_runner(
    runs_on,
    variables,
    *,
    event_name='push',
    head_repo='codemyriad/cassini-browser-protocol',
    repository='codemyriad/cassini-browser-protocol',
):
    """Evaluate a job's runs-on expression under GitHub Actions event, repo, and vars contexts."""
    if not (runs_on.startswith('${{') and runs_on.endswith('}}')):
        return runs_on
    expr = runs_on.removeprefix('${{').removesuffix('}}').strip()
    expr = expr.replace('&&', ' and ').replace('||', ' or ')
    repo_obj = _NULL if head_repo is None else SimpleNamespace(full_name=_CIString(head_repo))
    event = SimpleNamespace(pull_request=SimpleNamespace(head=SimpleNamespace(repo=repo_obj)))
    github = SimpleNamespace(
        event_name=_CIString(event_name),
        repository=_CIString(repository),
        event=event if event_name in ('pull_request', 'pull_request_target') else _NULL,
    )
    vars_ns = SimpleNamespace(CM_RUNNER=_CIString(variables.get('CM_RUNNER') or ''))
    return eval(expr, {'__builtins__': {}}, {'github': github, 'vars': vars_ns, 'fromJSON': json.loads})


class CiHygieneTests(unittest.TestCase):
    def test_ci_runs_on_pull_requests_and_on_pushes_to_main_only(self):
        # A branch push with an open PR used to run the whole contract twice; PRs also listen for
        # labeled (for full-ci) and nightly/manual runs exercise tight host-clock tolerances.
        trigger = re.search(r'^on:(.*?)^permissions:', (WORKFLOWS / 'ci.yml').read_text(), re.M | re.S)
        self.assertEqual(
            trigger and trigger.group(1),
            "\n  push:\n    branches: [main]\n"
            "  pull_request:\n    types: [opened, synchronize, reopened, labeled]\n"
            "  schedule:\n    - cron: '58 2 * * *'\n"
            "  workflow_dispatch:\n",
        )

    def test_only_superseded_pull_request_runs_are_cancelled(self):
        # Each main push, schedule, and dispatch has its own group (run_id): a shared group would drop
        # a pending main run. Labeled PR runs use a non-cancelling <pr>-label-<run_id> group so
        # opened/synchronize + labeled on the same head never leave CANCELLED checks (UNSTABLE).
        text = (WORKFLOWS / 'ci.yml').read_text()
        self.assertIn(
            "\nconcurrency:\n"
            "  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && "
            "(github.event.action == 'labeled' && format('{0}-label-{1}', github.event.pull_request.number, github.run_id) "
            "|| github.event.pull_request.number) || github.run_id }}\n"
            "  cancel-in-progress: ${{ github.event_name == 'pull_request' && github.event.action != 'labeled' }}\n",
            text,
        )

    def test_perf_gate_and_labeled_pr_isolation_across_full_dag(self):
        lp = _load_launcher_paths()
        text = (WORKFLOWS / 'ci.yml').read_text()
        header = text.split('\njobs:\n', 1)[0]
        group_expr = re.search(r'^  group: (.+)$', header, re.M).group(1)
        cancel_expr = re.search(r'^  cancel-in-progress: (.+)$', header, re.M).group(1)
        perf_expr = re.search(r'^  CASSINI_PERF_GATE: (.+)$', header, re.M).group(1)
        ci_jobs = dict(jobs(WORKFLOWS / 'ci.yml'))

        def make_github(event_name, *, action='', added_label='', labels=(), pr_number=42, run_id=900):
            if event_name == 'pull_request':
                pr_ns = SimpleNamespace(
                    number=pr_number,
                    labels=SimpleNamespace(_star_name=[_CIString(x) for x in labels]),
                )
                event_ns = SimpleNamespace(
                    action=_CIString(action),
                    label=SimpleNamespace(name=_CIString(added_label)),
                    pull_request=pr_ns,
                )
            else:
                event_ns = _NULL
            return SimpleNamespace(
                workflow=_CIString('Shared recording contract'),
                event_name=_CIString(event_name),
                run_id=run_id,
                event=event_ns,
            )

        def eval_expr(raw, github_ctx, needs_ctx=None):
            expr = raw.strip()
            if expr.startswith('${{') and expr.endswith('}}'):
                expr = expr.removeprefix('${{').removesuffix('}}').strip()
            expr = expr.replace('github.event.pull_request.labels.*.name', 'github.event.pull_request.labels._star_name')
            expr = re.sub(r'needs\.([A-Za-z0-9_-]+)', lambda m: f"needs.{m.group(1).replace('-', '_')}", expr)
            expr = expr.replace('&&', ' and ').replace('||', ' or ')
            return eval(
                expr,
                {'__builtins__': {}},
                {
                    'github': github_ctx,
                    'needs': needs_ctx or SimpleNamespace(),
                    'format': lambda tmpl, *args: str(tmpl).format(*args),
                    'contains': lambda items, needle: (
                        bool(items) and any(_CIString(x) == _CIString(needle) for x in items)
                    ),
                },
            )

        def eval_template(raw, github_ctx):
            return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(eval_expr(m.group(1), github_ctx)), raw)

        # Concurrency isolation across events
        self.assertEqual(
            eval_template(group_expr, make_github('pull_request', action='opened', pr_number=55, run_id=1)),
            'Shared recording contract-55',
        )
        self.assertEqual(
            eval_template(group_expr, make_github('pull_request', action='synchronize', pr_number=55, run_id=2)),
            'Shared recording contract-55',
        )
        self.assertEqual(
            eval_template(group_expr, make_github('pull_request', action='labeled', added_label='full-ci', pr_number=55, run_id=3)),
            'Shared recording contract-55-label-3',
        )
        self.assertEqual(
            eval_template(group_expr, make_github('push', run_id=77)),
            'Shared recording contract-77',
        )
        self.assertEqual(
            eval_template(group_expr, make_github('schedule', run_id=78)),
            'Shared recording contract-78',
        )
        self.assertTrue(bool(eval_expr(cancel_expr, make_github('pull_request', action='synchronize'))))
        self.assertFalse(bool(eval_expr(cancel_expr, make_github('pull_request', action='labeled', added_label='full-ci'))))
        self.assertFalse(bool(eval_expr(cancel_expr, make_github('push'))))
        self.assertFalse(bool(eval_expr(cancel_expr, make_github('schedule'))))
        self.assertFalse(bool(eval_expr(cancel_expr, make_github('workflow_dispatch'))))

        def parse_needs(block):
            m_inline = re.search(r'^    needs:\s*\[([^\]]+)\]\s*$', block, re.M)
            if m_inline:
                return [x.strip() for x in m_inline.group(1).split(',') if x.strip()]
            m_single = re.search(r'^    needs:\s*([A-Za-z0-9_-]+)\s*$', block, re.M)
            return [m_single.group(1)] if m_single else []

        def simulate_dag(event_name, *, action='', added_label='', labels=(), changed_files=('README.md',)):
            gh_ctx = make_github(event_name, action=action, added_label=added_label, labels=labels)
            perf_gate = str(eval_expr(perf_expr, gh_ctx))
            if event_name != 'pull_request' or perf_gate == '1':
                lp_out = {'launcher': True, 'capture': True, 'changed': True}
            else:
                lp_out = lp.classify_changes(list(changed_files))

            results = {}
            active = []
            for jname, block in ci_jobs.items():
                ancs = parse_needs(block)
                anc_ok = all(results[a] == 'success' for a in ancs)
                m_if = re.search(r'^    if:\s*(.+)$', block, re.M)
                cond = m_if.group(1) if m_if else None
                has_status_fn = bool(cond and any(fn in cond for fn in ('always()', 'cancelled()', 'failure()', 'success()')))
                if not anc_ok and not has_status_fn:
                    results[jname] = 'skipped'
                    continue
                needs_ns = SimpleNamespace(
                    launcher_paths=SimpleNamespace(
                        result=_CIString(results.get('launcher-paths', 'skipped')),
                        outputs=SimpleNamespace(
                            **{
                                k: _CIString(str(v).lower()) if results.get('launcher-paths') == 'success' else _CIString('')
                                for k, v in lp_out.items()
                            }
                        ),
                    ),
                    contract=SimpleNamespace(result=_CIString(results.get('contract', 'skipped'))),
                )
                ok = anc_ok if cond is None else bool(eval_expr(cond, gh_ctx, needs_ns))
                if ok:
                    results[jname] = 'success'
                    active.append(jname)
                else:
                    results[jname] = 'skipped'
            return perf_gate, active

        # Unlabeled docs PR skips launcher-macos and runs contract + nextcloud-talk with CASSINI_PERF_GATE == ''.
        self.assertEqual(
            simulate_dag('pull_request', action='opened', changed_files=('README.md',)),
            ('', ['launcher-paths', 'contract', 'nextcloud-talk']),
        )
        # Unlabeled capture PR runs all 4 jobs with CASSINI_PERF_GATE == ''.
        self.assertEqual(
            simulate_dag('pull_request', action='synchronize', changed_files=('extension/capture.js',)),
            ('', ['launcher-paths', 'launcher-macos', 'contract', 'nextcloud-talk']),
        )
        # full-ci labeled PR runs all 4 jobs (even on docs-only diff) with CASSINI_PERF_GATE == '1'.
        self.assertEqual(
            simulate_dag('pull_request', action='labeled', added_label='full-ci', labels=('full-ci',), changed_files=('README.md',)),
            ('1', ['launcher-paths', 'launcher-macos', 'contract', 'nextcloud-talk']),
        )
        # Unrelated labeled PR skips every job cleanly without ancestor-skip collateral on other runs.
        for other_label in ('bug', 'docs', 'ui-shots'):
            with self.subTest(other_label=other_label):
                self.assertEqual(
                    simulate_dag('pull_request', action='labeled', added_label=other_label, labels=(other_label,), changed_files=('extension/capture.js',)),
                    ('', []),
                )
        # Push to main runs all 4 jobs without perf gate; schedule and workflow_dispatch run all 4 with CASSINI_PERF_GATE == '1'.
        self.assertEqual(
            simulate_dag('push', changed_files=('README.md',)),
            ('', ['launcher-paths', 'launcher-macos', 'contract', 'nextcloud-talk']),
        )
        for full_event in ('schedule', 'workflow_dispatch'):
            with self.subTest(full_event=full_event):
                self.assertEqual(
                    simulate_dag(full_event, changed_files=('README.md',)),
                    ('1', ['launcher-paths', 'launcher-macos', 'contract', 'nextcloud-talk']),
                )

    def test_talk_archives_are_verified_cached_before_stack_creation(self):
        text = (WORKFLOWS / 'ci.yml').read_text()
        restore = text.index('Restore checksum-addressed Talk archives')
        verify = text.index('Verify or fetch the locked archives before starting Docker')
        save = text.index('Save verified Talk archives')
        start = text.index('Start a locked Nextcloud 34 and Talk 24 stack')
        self.assertLess(restore, verify)
        self.assertLess(verify, save)
        self.assertLess(save, start)
        self.assertNotIn('restore-keys:', text)
        self.assertIn('CASSINI_COMPAT_APP_CACHE:', text)
        self.assertIn('git -C talk-harness apply --check', text)
        self.assertIn('PROJECT_NAME: cassini-extension-nextcloud-${{ github.run_id }}-${{ github.run_attempt }}', text)
        self.assertIn("if: always() && steps.talk-stack.outputs.started == 'true'", text)

    def test_every_job_has_a_timeout(self):
        # GitHub's default is 360 min: a hung job holds a runner for six hours.
        missing = [f'{workflow.name}:{name}' for workflow in sorted(WORKFLOWS.glob('*.yml'))
                   for name, block in jobs(workflow)
                   if not re.search(r'^    timeout-minutes: \d+$', block, re.M)]
        self.assertEqual(missing, [])

    def test_runner_variable_only_selects_constant_labels(self):
        variable = [(name, value) for name, value in runs_on_values() if 'vars.' in value]
        self.assertEqual([name for name, _ in variable], ['ci.yml'])
        for name, value in variable:
            self.assertRegex(value, RUNNER_SELECTOR, name)
            self.assertNotIn('fromJSON(vars', value, name)

    def test_every_runs_on_is_a_hosted_image_or_the_selector(self):
        # Nothing else may reach runs-on: no literal self-hosted labels, no variable in a block-style runs-on.
        hosted = re.compile(r'^(?:ubuntu|macos|windows)-[0-9a-z.-]+$')
        for name, value in runs_on_values():
            with self.subTest(workflow=name):
                self.assertTrue(hosted.match(value) or RUNNER_SELECTOR.match(value), value)

    def test_runner_selection_falls_back_to_github_for_every_other_value_and_for_fork_prs(self):
        for name, value in runs_on_values():
            if 'vars.' not in value:
                continue
            for event_name, head_repo in (
                ('push', None),
                ('schedule', None),
                ('workflow_dispatch', None),
                ('pull_request', 'codemyriad/cassini-browser-protocol'),
            ):
                for setting in (None, '', ' ', 'garbage{', '["self-hosted","cm-hetzner"]',
                                '["self-hosted","cm-hetzne"]', 'hetzner-typo', 'true', 'github'):
                    variables = {} if setting is None else {'CM_RUNNER': setting}
                    with self.subTest(workflow=name, event=event_name, value=setting):
                        self.assertEqual(
                            select_runner(value, variables, event_name=event_name, head_repo=head_repo),
                            'ubuntu-24.04',
                        )
                for setting in ('hetzner', 'Hetzner'):
                    with self.subTest(workflow=name, event=event_name, value=setting):
                        self.assertEqual(
                            select_runner(value, {'CM_RUNNER': setting}, event_name=event_name, head_repo=head_repo),
                            CM_HETZNER,
                        )
            for untrusted_head in ('fork/cassini-browser-protocol', 'codemyriad/other-repo', '', None):
                with self.subTest(workflow=name, untrusted_head=untrusted_head):
                    self.assertEqual(
                        select_runner(
                            value,
                            {'CM_RUNNER': 'hetzner'},
                            event_name='pull_request',
                            head_repo=untrusted_head,
                        ),
                        'ubuntu-24.04',
                    )
            for prohibited_event in (
                'pull_request_target',
                'workflow_run',
                'issue_comment',
                'pull_request_review',
                'pull_request_review_comment',
                'discussion_comment',
                'release',
                'merge_group',
            ):
                with self.subTest(workflow=name, event=prohibited_event):
                    self.assertEqual(
                        select_runner(
                            value,
                            {'CM_RUNNER': 'hetzner'},
                            event_name=prohibited_event,
                            head_repo='codemyriad/cassini-browser-protocol',
                        ),
                        'ubuntu-24.04',
                    )

    def test_launcher_macos_workflow_gates_each_step_on_its_own_output(self):
        ci_jobs = dict(jobs(WORKFLOWS / 'ci.yml'))
        gate = ci_jobs['launcher-paths']
        macos = ci_jobs['launcher-macos']
        for out in ('changed', 'launcher', 'capture'):
            self.assertIn(f'{out}: ${{{{ steps.paths.outputs.{out} }}}}', gate)
        self.assertIn('python3 tests/launcher_paths.py --github-output "$GITHUB_OUTPUT"', gate)
        self.assertIn("if: needs.launcher-paths.outputs.changed == 'true'", macos)
        self.assertIn(
            "- name: Live-folder driver and control relay unit tests on macOS\n"
            "        if: needs.launcher-paths.outputs.launcher == 'true'\n"
            "        run: node --test tests/live-folder-driver.test.mjs tests/control-relay.test.mjs",
            macos,
        )
        self.assertIn(
            "- name: A capture over 30 s keeps real time (D68.1)\n"
            "        if: needs.launcher-paths.outputs.capture == 'true'\n"
            "        run: |\n"
            "          npm ci",
            macos,
        )

    def test_launcher_paths_covers_every_transitive_import_and_skips_unrelated_changes(self):
        lp = _load_launcher_paths()

        # Walk live JS relative imports of both macOS test entrypoints so new imports cannot slip past.
        queue = [ROOT / 'tests/live-folder-driver.test.mjs', ROOT / 'tests/control-relay.test.mjs']
        seen_launcher_js = set()
        while queue:
            cur = queue.pop(0).resolve()
            rel = cur.relative_to(ROOT).as_posix()
            if rel in seen_launcher_js:
                continue
            seen_launcher_js.add(rel)
            for match in re.finditer(r"""from\s+['"](\.[^'"]+)['"]""", cur.read_text(encoding='utf-8')):
                target = (cur.parent / match.group(1)).resolve()
                queue.append(target)
        for rel in sorted(seen_launcher_js):
            with self.subTest(imported=rel):
                self.assertEqual(
                    lp.classify_changes([rel]),
                    {'launcher': True, 'capture': False, 'changed': True},
                )

        # Live-folder oracle files (except markdown docs) trigger launcher only, never 45s capture-real-time.
        for rel in ('conformance/live-folder/check.py', 'conformance/live-folder/make.py',
                    'conformance/live-folder/scenario.json', 'conformance/multistream/corpus.json',
                    'conformance/check-session-events.py', 'conformance/gaps/gaps.py', 'conformance/pcm.py',
                    'schemas/session.schema.json'):
            with self.subTest(oracle_input=rel):
                self.assertEqual(
                    lp.classify_changes([rel]),
                    {'launcher': True, 'capture': False, 'changed': True},
                )
        self.assertEqual(
            lp.classify_changes(['conformance/live-folder/README.md', 'TESTING.md', 'SHA256SUMS']),
            {'launcher': False, 'capture': False, 'changed': False},
        )

        # Capture real-time and npm install inputs trigger capture only, never launcher unit tests.
        for rel in ('extension/capture.js', 'extension/pcm-worklet.js', 'tests/capture-real-time.mjs',
                    'package-lock.json', 'npm-shrinkwrap.json', 'binding.gyp', '.npmrc', 'sub/.npmrc'):
            with self.subTest(capture_input=rel):
                self.assertEqual(
                    lp.classify_changes([rel]),
                    {'launcher': False, 'capture': True, 'changed': True},
                )

        # package.json ordinary test-script edits (create, modify, delete) skip launcher-macos completely,
        # whereas npm lifecycle hooks (preinstall, install, postinstall, prepare) and dependency/type
        # edits trigger the affected macOS step(s).
        base_pkg = (ROOT / 'package.json').read_text(encoding='utf-8')
        scripts_modified = json.loads(base_pkg)
        scripts_modified['scripts']['test'] += ' && node tests/new-linux-test.mjs'
        scripts_created = json.loads(base_pkg)
        scripts_created['scripts']['test:bfcache'] = 'node tests/bfcache.mjs'
        scripts_deleted = json.loads(base_pkg)
        del scripts_deleted['scripts']['test:signals']
        for label, candidate in (
            ('modify-test', scripts_modified),
            ('create-test-colon', scripts_created),
            ('delete-test-colon', scripts_deleted),
        ):
            with self.subTest(script_case=label):
                self.assertEqual(
                    lp.classify_changes(
                        ['package.json', 'tests/bfcache.mjs', 'SHA256SUMS'],
                        read_rev_file=lambda rev, _p, c=candidate: base_pkg if rev == 'B' else json.dumps(c),
                        base_rev='B',
                        head_rev='H',
                    ),
                    {'launcher': False, 'capture': False, 'changed': False},
                )

        for hook in ('preinstall', 'install', 'postinstall', 'preprepare', 'prepare', 'postprepare', 'prepublish'):
            with_hook = json.loads(base_pkg)
            with_hook['scripts'][hook] = 'node scripts/hook-v1.mjs'
            modified_hook = json.loads(base_pkg)
            modified_hook['scripts'][hook] = 'node scripts/hook-v2.mjs'
            with self.subTest(hook=hook, op='create'):
                self.assertEqual(
                    lp.classify_changes(
                        ['package.json'],
                        read_rev_file=lambda rev, _p, wh=with_hook: base_pkg if rev == 'B' else json.dumps(wh),
                        base_rev='B',
                        head_rev='H',
                    ),
                    {'launcher': False, 'capture': True, 'changed': True},
                )
            with self.subTest(hook=hook, op='modify'):
                self.assertEqual(
                    lp.classify_changes(
                        ['package.json'],
                        read_rev_file=lambda rev, _p, wh=with_hook, mh=modified_hook: (
                            json.dumps(wh) if rev == 'B' else json.dumps(mh)
                        ),
                        base_rev='B',
                        head_rev='H',
                    ),
                    {'launcher': False, 'capture': True, 'changed': True},
                )
            with self.subTest(hook=hook, op='delete'):
                self.assertEqual(
                    lp.classify_changes(
                        ['package.json'],
                        read_rev_file=lambda rev, _p, wh=with_hook: json.dumps(wh) if rev == 'B' else base_pkg,
                        base_rev='B',
                        head_rev='H',
                    ),
                    {'launcher': False, 'capture': True, 'changed': True},
                )

        deps_pkg = json.loads(base_pkg)
        deps_pkg['devDependencies']['playwright'] = '1.99.0'
        self.assertEqual(
            lp.classify_changes(
                ['package.json'],
                read_rev_file=lambda rev, _p: base_pkg if rev == 'B' else json.dumps(deps_pkg),
                base_rev='B',
                head_rev='H',
            ),
            {'launcher': False, 'capture': True, 'changed': True},
        )

        # Editing contract or nextcloud-talk in ci.yml skips launcher-macos; editing launcher-macos runs both.
        base_ci = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')
        contract_ci = base_ci.replace('timeout-minutes: 25', 'timeout-minutes: 30', 1)
        self.assertNotEqual(base_ci, contract_ci)
        self.assertEqual(
            lp.classify_changes(
                ['.github/workflows/ci.yml'],
                read_rev_file=lambda rev, _p: base_ci if rev == 'B' else contract_ci,
                base_rev='B',
                head_rev='H',
            ),
            {'launcher': False, 'capture': False, 'changed': False},
        )
        macos_ci = base_ci.replace('runs-on: macos-15', 'runs-on: macos-15 # touched', 1)
        self.assertEqual(
            lp.classify_changes(
                ['.github/workflows/ci.yml'],
                read_rev_file=lambda rev, _p: base_ci if rev == 'B' else macos_ci,
                base_rev='B',
                head_rev='H',
            ),
            {'launcher': True, 'capture': True, 'changed': True},
        )

    def test_launcher_paths_git_evaluation_handles_unknown_base_and_synthetic_pr_merges(self):
        lp = _load_launcher_paths()
        with tempfile.TemporaryDirectory() as temp:
            repo = pathlib.Path(temp)
            env = {
                **os.environ,
                'GIT_AUTHOR_NAME': 'Test',
                'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                'GIT_COMMITTER_NAME': 'Test',
                'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            }

            def git(*args):
                return subprocess.check_output(
                    ['git', '-C', str(repo), *args], text=True, stderr=subprocess.DEVNULL, env=env
                ).strip()

            git('init', '-b', 'main')
            (repo / 'README.md').write_text('v1\n', encoding='utf-8')
            (repo / '.github/workflows').mkdir(parents=True)
            base_ci = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')
            (repo / '.github/workflows/ci.yml').write_text(base_ci, encoding='utf-8')
            (repo / 'package.json').write_text((ROOT / 'package.json').read_text(encoding='utf-8'), encoding='utf-8')
            (repo / 'extension').mkdir()
            (repo / 'extension/capture.js').write_text('// v1\n', encoding='utf-8')
            (repo / 'scenarios').mkdir()
            (repo / 'scenarios/native-launcher.mjs').write_text('// launcher v1\n', encoding='utf-8')
            git('add', '.')
            git('commit', '-m', 'init')
            base_sha = git('rev-parse', 'HEAD')

            # Unknown or zero base runs both safely.
            res, resolved = lp.evaluate_git(repo, event_name='pull_request', base_sha='0' * 40)
            self.assertIsNone(resolved)
            self.assertEqual(res, {'launcher': True, 'capture': True, 'changed': True})

            # Pushes to main ALWAYS return all outputs true, even with a known base on docs-only or workflow-only commits.
            (repo / 'README.md').write_text('docs push on main\n', encoding='utf-8')
            git('commit', '-am', 'docs push')
            docs_push_sha = git('rev-parse', 'HEAD')
            res, resolved = lp.evaluate_git(repo, event_name='push', base_sha=base_sha, head=docs_push_sha)
            self.assertIsNone(resolved)
            self.assertEqual(res, {'launcher': True, 'capture': True, 'changed': True})

            contract_ci = base_ci.replace('timeout-minutes: 25', 'timeout-minutes: 30', 1)
            (repo / '.github/workflows/ci.yml').write_text(contract_ci, encoding='utf-8')
            git('commit', '-am', 'workflow push')
            wf_push_sha = git('rev-parse', 'HEAD')
            res, resolved = lp.evaluate_git(repo, event_name='push', base_sha=docs_push_sha, head=wf_push_sha)
            self.assertIsNone(resolved)
            self.assertEqual(res, {'launcher': True, 'capture': True, 'changed': True})
            git('reset', '--hard', base_sha)

            # Real Git PR lifecycle (creation, modification, deletion, rename-out-of-gate) for:
            # - launcher input (scenarios/native-launcher.mjs)
            # - capture input (extension/capture.js)
            # - .npmrc, npm-shrinkwrap.json, and binding.gyp
            # - package.json installation lifecycle hook (postinstall)
            def assert_pr_commit(commit_base, expected):
                head_sha = git('rev-parse', 'HEAD')
                actual, actual_base = lp.evaluate_git(
                    repo, event_name='pull_request', base_sha=commit_base, head=head_sha
                )
                self.assertEqual(actual_base, commit_base)
                self.assertEqual(actual, expected)

            for gated_path, moved_path, expected in (
                (
                    'conformance/live-folder/new-case.json',
                    'docs-untracked-case.txt',
                    {'launcher': True, 'capture': False, 'changed': True},
                ),
                (
                    '.npmrc',
                    'npmrc.disabled',
                    {'launcher': False, 'capture': True, 'changed': True},
                ),
                (
                    'npm-shrinkwrap.json',
                    'shrinkwrap.disabled',
                    {'launcher': False, 'capture': True, 'changed': True},
                ),
                (
                    'binding.gyp',
                    'binding.gyp.disabled',
                    {'launcher': False, 'capture': True, 'changed': True},
                ),
            ):
                with self.subTest(git_lifecycle=gated_path):
                    git('checkout', '-B', 'case', base_sha)
                    target = repo / gated_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 1. Creation
                    target.write_text('{"v": 1}\n', encoding='utf-8')
                    git('add', gated_path)
                    git('commit', '-m', f'create {gated_path}')
                    created_sha = git('rev-parse', 'HEAD')
                    assert_pr_commit(base_sha, expected)
                    # 2. Modification
                    target.write_text('{"v": 2}\n', encoding='utf-8')
                    git('commit', '-am', f'modify {gated_path}')
                    modified_sha = git('rev-parse', 'HEAD')
                    assert_pr_commit(created_sha, expected)
                    # 3. Rename out of gate (--no-renames -z must still report the removed gated source path!)
                    git('mv', gated_path, moved_path)
                    git('commit', '-m', f'rename {gated_path} -> {moved_path}')
                    assert_pr_commit(modified_sha, expected)
                    # 4. Deletion
                    git('checkout', '-B', 'case-del', modified_sha)
                    git('rm', gated_path)
                    git('commit', '-m', f'delete {gated_path}')
                    assert_pr_commit(modified_sha, expected)

            # Rename-out-of-gate and deletion on existing tracked launcher and capture inputs.
            for existing_path, moved_path, expected in (
                (
                    'scenarios/native-launcher.mjs',
                    'scenarios/untracked-helper.mjs',
                    {'launcher': True, 'capture': False, 'changed': True},
                ),
                (
                    'extension/capture.js',
                    'extension/capture-moved.js',
                    {'launcher': False, 'capture': True, 'changed': True},
                ),
            ):
                with self.subTest(rename_existing=existing_path):
                    git('checkout', '-B', 'case-existing', base_sha)
                    (repo / existing_path).write_text('// modified\n', encoding='utf-8')
                    git('commit', '-am', f'modify {existing_path}')
                    assert_pr_commit(base_sha, expected)

                    git('checkout', '-B', 'case-rename', base_sha)
                    git('mv', existing_path, moved_path)
                    git('commit', '-m', f'rename {existing_path} -> {moved_path}')
                    assert_pr_commit(base_sha, expected)

                    git('checkout', '-B', 'case-delete', base_sha)
                    git('rm', existing_path)
                    git('commit', '-m', f'delete {existing_path}')
                    assert_pr_commit(base_sha, expected)

            # Real Git PR lifecycle for package.json installation lifecycle hook (postinstall) + rename-out + delete.
            git('checkout', '-B', 'pkg-hook', base_sha)
            pkg_obj = json.loads((repo / 'package.json').read_text(encoding='utf-8'))
            pkg_obj['scripts']['postinstall'] = 'node -e "process.exit(0)"'
            (repo / 'package.json').write_text(json.dumps(pkg_obj, indent=2) + '\n', encoding='utf-8')
            git('commit', '-am', 'add postinstall hook')
            hook_sha = git('rev-parse', 'HEAD')
            assert_pr_commit(base_sha, {'launcher': False, 'capture': True, 'changed': True})

            pkg_obj['scripts']['postinstall'] = 'node -e "console.log(1)"'
            (repo / 'package.json').write_text(json.dumps(pkg_obj, indent=2) + '\n', encoding='utf-8')
            git('commit', '-am', 'modify postinstall hook')
            assert_pr_commit(hook_sha, {'launcher': False, 'capture': True, 'changed': True})

            git('checkout', '-B', 'pkg-rename', base_sha)
            git('mv', 'package.json', 'package.json.disabled')
            git('commit', '-m', 'rename package.json out of gate')
            assert_pr_commit(base_sha, {'launcher': True, 'capture': True, 'changed': True})

            git('checkout', '-B', 'pkg-delete', base_sha)
            git('rm', 'package.json')
            git('commit', '-m', 'delete package.json')
            assert_pr_commit(base_sha, {'launcher': True, 'capture': True, 'changed': True})

            # PR branch changes only README.md, while main advances on extension/capture.js;
            # GitHub's synthetic PR merge commit (HEAD^1=main_tip, HEAD^2=pr_tip) must NOT
            # false-trigger launcher-macos on main's already-landed capture.js change.
            git('checkout', 'main')
            git('checkout', '-b', 'pr')
            (repo / 'README.md').write_text('v2 from pr\n', encoding='utf-8')
            git('commit', '-am', 'docs only')
            pr_tip = git('rev-parse', 'HEAD')

            git('checkout', 'main')
            (repo / 'extension/capture.js').write_text('// v2 on main\n', encoding='utf-8')
            git('commit', '-am', 'capture on main')
            main_tip = git('rev-parse', 'HEAD')

            git('checkout', '--detach', main_tip)
            git('merge', '--no-ff', '-m', 'Merge pr into main', pr_tip)
            res, resolved = lp.evaluate_git(repo, event_name='pull_request', base_sha=base_sha)
            self.assertEqual(resolved, main_tip)
            self.assertEqual(res, {'launcher': False, 'capture': False, 'changed': False})

            # Adding full-ci (perf_gate=True) to that same docs-only PR forces both macOS steps on.
            perf_res, perf_resolved = lp.evaluate_git(
                repo, event_name='pull_request', base_sha=base_sha, perf_gate=True
            )
            self.assertIsNone(perf_resolved)
            self.assertEqual(perf_res, {'launcher': True, 'capture': True, 'changed': True})
            gh_out = repo / 'github_output.txt'
            rc = lp.main(
                [
                    '--repo', str(repo),
                    '--event-name', 'pull_request',
                    '--base', base_sha,
                    '--perf-gate', '1',
                    '--github-output', str(gh_out),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                dict(line.split('=', 1) for line in gh_out.read_text(encoding='utf-8').strip().splitlines()),
                {'launcher': 'true', 'capture': 'true', 'changed': 'true'},
            )

    def test_workflows_do_not_call_unbounded_apt_get_directly(self):
        ci_text = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')
        self.assertNotRegex(ci_text, r'\bapt-get\b')
        self.assertIn('sudo bash tests/apt_install_bounded.sh pulseaudio xvfb xauth', ci_text)
        # Ensure mirror healing + 80cassini-network precede Playwright's --with-deps apt-get call in contract.
        contract_block = dict(jobs(WORKFLOWS / 'ci.yml'))['contract']
        self.assertLess(
            contract_block.index('tests/apt_install_bounded.sh'),
            contract_block.index('npx playwright install --with-deps chromium'),
        )

    def test_apt_install_bounded_retries_mirrors_including_kernel_org_and_never_retries_dpkg(self):
        script = ROOT / 'tests/apt_install_bounded.sh'
        for layout in ('direct', 'mirror-list'):
            for mode, ok, downloads, installs in (
                ('ok', True, 1, 1),
                ('fail-1', True, 2, 1),
                ('fail-2', True, 3, 1),
                ('fail-all', False, 3, 0),
                ('dpkg-fails', False, 1, 1),
            ):
                with self.subTest(layout=layout, mode=mode), tempfile.TemporaryDirectory() as temp:
                    tmp = pathlib.Path(temp)
                    bindir = tmp / 'bin'
                    bindir.mkdir()
                    fake_apt = bindir / 'apt-get'
                    fake_apt.write_text(
                        f'#!{sys.executable}\n'
                        'import json, os, sys\n'
                        'from pathlib import Path\n'
                        'log = Path(os.environ["APT_TEST_LOG"])\n'
                        'rows = json.loads(log.read_text()) if log.exists() else []\n'
                        'rows.append(sys.argv[1:])\n'
                        'log.write_text(json.dumps(rows))\n'
                        'mode = os.environ["APT_TEST_MODE"]\n'
                        'if "--download-only" in sys.argv:\n'
                        '    n = sum("--download-only" in r for r in rows)\n'
                        '    if mode == "fail-all" or (mode == "fail-1" and n == 1) or (mode == "fail-2" and n <= 2):\n'
                        '        sys.exit(100)\n'
                        'if "--no-download" in sys.argv and mode == "dpkg-fails":\n'
                        '    sys.exit(42)\n',
                        encoding='utf-8',
                    )
                    fake_apt.chmod(0o755)
                    apt_dir = tmp / 'apt'
                    sources_d = apt_dir / 'sources.list.d'
                    sources_d.mkdir(parents=True)
                    ubuntu_sources = sources_d / 'ubuntu.sources'
                    if layout == 'mirror-list':
                        mirrors = apt_dir / 'apt-mirrors.txt'
                        mirrors.write_text(
                            'http://azure.archive.ubuntu.com/ubuntu/\n'
                            'http://archive.ubuntu.com/ubuntu/\n'
                            'http://security.ubuntu.com/ubuntu/\n',
                            encoding='utf-8',
                        )
                        ubuntu_sources.write_text(
                            f'URIs: mirror+file:{mirrors}\nSuites: noble noble-updates\n\n'
                            'URIs: http://security.ubuntu.com/ubuntu/\nSuites: noble-security\n',
                            encoding='utf-8',
                        )
                    else:
                        ubuntu_sources.write_text(
                            'URIs: http://azure.archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates\n\n'
                            'URIs: http://security.ubuntu.com/ubuntu/\nSuites: noble-security\n',
                            encoding='utf-8',
                        )
                    other = sources_d / 'third-party.list'
                    other.write_text('deb https://example.invalid/repo stable main\n', encoding='utf-8')
                    log = tmp / 'calls.json'
                    res = subprocess.run(
                        ['bash', str(script), '--no-install-recommends', 'pulseaudio', 'xvfb'],
                        env={
                            **os.environ,
                            'PATH': f'{bindir}:{os.environ["PATH"]}',
                            'APT_TEST_LOG': str(log),
                            'APT_TEST_MODE': mode,
                            'CASSINI_APT_SOURCES_DIR': str(apt_dir),
                        },
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(res.returncode == 0, ok, res.stderr)
                    calls = json.loads(log.read_text(encoding='utf-8'))
                    self.assertEqual(sum('--download-only' in row for row in calls), downloads)
                    self.assertEqual(sum('--no-download' in row for row in calls), installs)
                    self.assertEqual(other.read_text(encoding='utf-8'), 'deb https://example.invalid/repo stable main\n')
                    policy = (apt_dir / 'apt.conf.d/80cassini-network').read_text(encoding='utf-8')
                    self.assertIn('Acquire::http::Timeout "15";', policy)
                    self.assertIn('APT::Update::Error-Mode "any";', policy)
                    if mode in ('fail-2', 'fail-all'):
                        self.assertIn('https://mirrors.edge.kernel.org/ubuntu/', ubuntu_sources.read_text(encoding='utf-8'))
                        self.assertNotIn('security.ubuntu.com', ubuntu_sources.read_text(encoding='utf-8'))
                        if layout == 'mirror-list':
                            self.assertEqual(
                                (apt_dir / 'apt-mirrors.txt').read_text(encoding='utf-8'),
                                'https://mirrors.edge.kernel.org/ubuntu/\n',
                            )
                    if mode == 'dpkg-fails':
                        self.assertEqual(res.returncode, 42)


if __name__ == '__main__':
    unittest.main()
