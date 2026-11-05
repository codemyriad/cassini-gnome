# SPDX-License-Identifier: GPL-3.0-or-later
"""D66: one CI run per PR push, superseded PR runs cancelled, main never cancelled, every job bounded."""
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


if __name__ == '__main__':
    unittest.main()
