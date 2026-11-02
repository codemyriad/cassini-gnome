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
        self.assertEqual(kinds, {'track', 'audio', 'sources', 'trackEnd', 'stop', 'control', 'documentChanged'})

    def test_mutants_fail(self):
        boundary = {'pcm-then-silence-closes-at-exactly-45-seconds', 'control-frames-do-not-count',
                    'audio-frame-arriving-at-the-deadline-is-too-late'}
        self.assertLessEqual(boundary, failing(mutant('return now - last >= PCM_STALE_AFTER', 'return now - last > PCM_STALE_AFTER')))
        self.assertIn('pcm-then-silence-closes-at-exactly-45-seconds',
                      failing(mutant('PCM_STALE_AFTER = 45000', 'PCM_STALE_AFTER = 40000')))
        self.assertIn('audio-frame-arriving-at-the-deadline-is-too-late',
                      failing(mutant('return True  # a frame at or after the deadline arrives too late', 'pass')))
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

    def test_document_retirement_is_normative_and_mutants_fail(self):
        # D57: a gone document's spans never arm the watchdog; the next one's do.
        self.assertIn('`documentChanged` frames', (ROOT / 'SPEC.md').read_text())
        case = 'gone-documents-spans-are-retired-and-the-next-documents-spans-arm'
        mutants = {
            'navigating keeps the open spans': ('                open_spans.clear()\n                departing = True', '                departing = True'),
            'no departing window': ("            if not departing:\n                open_spans.add(frame['id'])", "            open_spans.add(frame['id'])"),
            'only a generation closes the window': ("if page == 'present' or document is not None:", 'if document is not None:'),
            'the window never closes': ('                departing = False\n', '                pass\n'),
            'a new generation retires nothing': ('open_spans.difference_update(declared[:confirmed])', 'pass'),
            "a new generation retires the next document's spans": ('difference_update(declared[:confirmed])', 'difference_update(declared)'),
            'confirmation never moves': ('generation, confirmed = document, len(declared)', 'generation = document'),
            'documentChanged is activity': ('            continue  # a control frame: never activity',
                                            "            last = frame['atMs']\n            continue"),
            'a retired span re-arms on its PCM': ("elif kind == 'audio' and frame['id'] in open_spans:", "elif kind == 'audio':\n            open_spans.add(frame['id'])"),
        }
        for name, (old, new) in mutants.items():
            with self.subTest(mutant=name):
                self.assertIn(case, failing(mutant(old, new)))


if __name__ == '__main__':
    unittest.main()
