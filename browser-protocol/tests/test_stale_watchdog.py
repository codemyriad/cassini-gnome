"""D52.5 amendment: PCM_STALE_AFTER = 45 s with a normative >= comparison."""
import importlib.util
import json
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / 'conformance/multistream/stale_watchdog.py'
VECTORS = json.loads((ROOT / 'conformance/multistream/stale-watchdog.json').read_text())


def load(source=None):
    namespace = {}
    exec(compile(source if source is not None else ORACLE.read_text(), str(ORACLE), 'exec'), namespace)
    return namespace


def mutant(old, new):
    source = ORACLE.read_text()
    assert source.count(old) == 1, old
    return load(source.replace(old, new))


def failing(oracle):
    names = set()
    for case in VECTORS['cases']:
        checks = [(p['atMs'], p['closed']) for p in case['probes']]
        if case['closeAtMs'] is not None:
            checks += [(case['closeAtMs'] - 1, False), (case['closeAtMs'], True)]
        if any(oracle['closed'](case['frames'], at) != want for at, want in checks):
            names.add(case['name'])
    return names


class StaleWatchdogTests(unittest.TestCase):
    def test_constant_and_spec_are_normative(self):
        self.assertEqual(VECTORS['constants'], {'PCM_STALE_AFTER': 45000})
        spec = (ROOT / 'SPEC.md').read_text()
        self.assertIn('`PCM_STALE_AFTER = 45 s`', spec)
        self.assertIn('now - lastAudioTransportFrame >= 45 s', spec)
        self.assertIn('stale-watchdog.json', spec)

    def test_oracle_reproduces_every_literal_case(self):
        oracle = load()
        for case in VECTORS['cases']:
            with self.subTest(case=case['name']):
                frames = case['frames']
                self.assertEqual([f['atMs'] for f in frames], sorted(f['atMs'] for f in frames))
                self.assertTrue(case['probes'])
        self.assertEqual(failing(oracle), set())
        kinds = {f['frame'] for case in VECTORS['cases'] for f in case['frames']}
        self.assertEqual(kinds, {'track', 'audio', 'sources', 'trackEnd', 'stop', 'control'})

    def test_mutants_fail(self):
        boundary = {'pcm-then-silence-closes-at-exactly-45-seconds', 'control-frames-do-not-count'}
        self.assertLessEqual(boundary, failing(mutant('return now - last >= PCM_STALE_AFTER', 'return now - last > PCM_STALE_AFTER')))
        self.assertIn('pcm-then-silence-closes-at-exactly-45-seconds',
                      failing(mutant('PCM_STALE_AFTER = 45000', 'PCM_STALE_AFTER = 40000')))
        self.assertIn('track-only-frames-keep-the-transport-alive',
                      failing(mutant("{'track', 'audio', 'sources', 'trackEnd', 'stop'}", "{'audio', 'sources', 'trackEnd', 'stop'}")))
        self.assertIn('sources-only-frames-keep-the-transport-alive',
                      failing(mutant("{'track', 'audio', 'sources', 'trackEnd', 'stop'}", "{'track', 'audio', 'trackEnd', 'stop'}")))
        self.assertIn('trackEnd-counts-as-a-frame-while-another-span-stays-armed',
                      failing(mutant("        last, kind = frame['atMs'], frame['frame']\n",
                                     "        kind = frame['frame']\n        if kind != 'trackEnd': last = frame['atMs']\n")))
        self.assertIn('control-frames-do-not-count',
                      failing(mutant("{'track', 'audio', 'sources', 'trackEnd', 'stop'}", "{'track', 'audio', 'sources', 'trackEnd', 'stop', 'control'}")))
        armed_on_declaration = failing(mutant('return bool(open_spans & delivered)', 'return bool(open_spans)'))
        self.assertIn('declared-span-without-pcm-is-unarmed-until-its-first-pcm', armed_on_declaration)
        self.assertIn('new-span-rearms-only-after-its-pcm', armed_on_declaration)
        sticky = failing(mutant('return bool(open_spans & delivered)', 'return bool(delivered)'))
        self.assertIn('last-armed-span-ended-disarms', sticky)
        self.assertIn('stop-disarms', sticky)


if __name__ == '__main__':
    unittest.main()
