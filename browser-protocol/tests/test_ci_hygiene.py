# SPDX-License-Identifier: GPL-3.0-or-later
"""D66: one CI run per PR push, superseded PR runs cancelled, main never cancelled, every job bounded."""
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github/workflows'


def jobs(workflow):
    body = workflow.read_text().split('\njobs:\n', 1)[1]
    for block in re.split(r'\n(?=  [A-Za-z0-9_-]+:\n)', '\n' + body):
        if match := re.match(r'\s*([A-Za-z0-9_-]+):\n', block):
            yield match.group(1), block


# CM_RUNNER moves a job to the cm-hetzner runners only when it is exactly "hetzner". The labels are a
# constant and the variable is never parsed, so any other value (unset, empty, a typo, JSON) keeps the
# GitHub-hosted image instead of failing the expression or queueing for labels no runner has.
CM_HETZNER = ['self-hosted', 'cm-hetzner']
RUNNER_SELECTOR = re.compile(
    r"^\$\{\{ vars\.CM_RUNNER == 'hetzner' && "
    r"fromJSON\('(?P<labels>\[\"self-hosted\",\"cm-hetzner\"\])'\) \|\| '(?P<default>ubuntu-24\.04)' \}\}$")


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


def select_runner(runs_on, variables):
    """What GitHub picks for runs-on given the repository variables: a model of the expression
    above (`==` compares strings case-insensitively; an unset variable reads as '')."""
    match = RUNNER_SELECTOR.match(runs_on)
    if match is None:
        return runs_on
    on = (variables.get('CM_RUNNER') or '').casefold() == 'hetzner'
    return json.loads(match['labels']) if on else match['default']


class CiHygieneTests(unittest.TestCase):
    def test_ci_runs_on_pull_requests_and_on_pushes_to_main_only(self):
        # A branch push with an open PR used to run the whole contract twice.
        trigger = re.search(r'^on:(.*?)^permissions:', (WORKFLOWS / 'ci.yml').read_text(), re.M | re.S)
        self.assertEqual(trigger and trigger.group(1), '\n  push:\n    branches: [main]\n  pull_request:\n')

    def test_only_superseded_pull_request_runs_are_cancelled(self):
        # Each main push has its own group (run_id): a shared group would drop a pending main run.
        text = (WORKFLOWS / 'ci.yml').read_text()
        self.assertIn('\nconcurrency:\n  group: ${{ github.workflow }}-${{ github.event_name == '
                      "'pull_request' && github.event.pull_request.number || github.run_id }}\n"
                      "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n", text)

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

    def test_runner_selection_falls_back_to_github_for_every_other_value(self):
        for name, value in runs_on_values():
            if 'vars.' not in value:
                continue
            for setting in (None, '', ' ', 'garbage{', '["self-hosted","cm-hetzner"]',
                            '["self-hosted","cm-hetzne"]', 'hetzner-typo', 'true'):
                variables = {} if setting is None else {'CM_RUNNER': setting}
                with self.subTest(workflow=name, value=setting):
                    self.assertEqual(select_runner(value, variables), 'ubuntu-24.04')
            for setting in ('hetzner', 'Hetzner'):
                with self.subTest(workflow=name, value=setting):
                    self.assertEqual(select_runner(value, {'CM_RUNNER': setting}), CM_HETZNER)


if __name__ == '__main__':
    unittest.main()
