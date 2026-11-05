"""Shared test 2 oracle self-test: the synthetic live folder, its mutants and check.py.

The base folder and every variant (another correct writer form) must pass every
check; each mutant must fail exactly the rules make.py lists for it, and every rule
check.py has must be shown failing by some mutant or evidence case, so a rule that
silently always passes turns this test red. check.py runs as the apps run it: a
subprocess under `python3 -I`, once more with no site-packages at all (stdlib only).
Its corpus constants are read from corpus.json, never copied.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / 'conformance/live-folder'
CHECK = LIVE / 'check.py'


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make = _module('live_folder_make', LIVE / 'make.py')
oracle = _module('live_folder_check', CHECK)
events = _module('live_folder_events', ROOT / 'conformance/check-session-events.py')
pcm = _module('live_folder_pcm', ROOT / 'conformance/pcm.py')


def run_check(folder, evidence, out, *flags):
    process = subprocess.run([sys.executable, '-I', *flags, str(CHECK), 'check', str(folder),
                              '--evidence', str(evidence), '--out', str(out)], capture_output=True, text=True)
    result = json.loads(out.read_text()) if out.exists() and process.returncode in (0, 1) else None
    return process, result


def failed(result):
    return [c['id'] for c in result['checks'] if not c['pass']], [r for c in result['checks'] for r in c['failed']]


# Evidence faults: (the change, every rule it must fail, exactly).
EVIDENCE_CASES = [
    (lambda e: e['slotC'].update(packetsReceived=12), ['O4.evidence']),
    (lambda e: e.update(maxSourceCount=2), ['O4.evidence']),
    (lambda e: e['actions']['kill'].update(requestedAtMs=96000, observedAtMs=96500),
     ['O3.lost', 'O3.restored', 'O3.state', 'O3.resumedStart', 'O3.system']),
    (lambda e: e['nativeTones'].update(microphone=440), ['O5.micAudio']),
    (lambda e: e.update(newFolders=e['newFolders'] * 2), ['O0.oneFolder']),
    (lambda e: e['actions']['stop'].update(requestedAtMs=112000, observedAtMs=112500), ['O6.lastFrame']),
    (lambda e: e['actions']['lateRemote'].update(requestedAtMs=71000, observedAtMs=72000), ['O1.bracket']),
]


class LiveFolderOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='cassini-live-folder-')
        cls.root = Path(cls.temporary.name) / 'made'  # only make.py writes here (digests)
        cls.work = Path(cls.temporary.name) / 'work'
        cls.work.mkdir()
        for name in [None] + sorted(make.MUTANTS) + sorted(make.VARIANTS):
            make.make(cls.root / (name or 'base'), name)
        cls.base = next((cls.root / 'base').glob('Recording-*'))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def copy(self, name='base'):
        """A writable copy of one materialized run: (folder, evidence path)."""
        target = Path(tempfile.mkdtemp(prefix='run-', dir=self.work))
        shutil.copytree(self.root / name, target / name)
        return next((target / name).glob('Recording-*')), target / name / 'evidence.json'

    def test_output_is_byte_identical_to_the_pinned_digests(self):
        lines = (LIVE / 'materialized.sha256').read_text().splitlines()
        pinned = dict(reversed(line.split('  ', 1)) for line in lines)
        made = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file())
        self.assertEqual(sorted(pinned), made)
        for relative, digest in pinned.items():
            self.assertEqual(hashlib.sha256((self.root / relative).read_bytes()).hexdigest(), digest, relative)

    def test_base_validates_and_passes_every_check(self):
        events.validate(json.loads((self.base / 'session.json').read_text()))
        process, result = run_check(self.base, self.root / 'base/evidence.json', self.work / 'base.result.json')
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(failed(result), ([], []))
        self.assertEqual([c['id'] for c in result['checks']], ['O0', 'O1', 'O2', 'O3', 'O4', 'O5', 'O6', 'O7'])
        lines = [json.loads(line) for line in process.stdout.splitlines()[:-1]]
        self.assertEqual([(line['id'], line['pass']) for line in lines], [(c['id'], True) for c in result['checks']])
        self.assertEqual(result['checks'][0]['measured']['O0.schema']['value'], 'validated')

    def test_check_needs_only_the_standard_library(self):
        # -S: no site-packages at all, so no jsonschema; the schema step says so and nothing else changes.
        process, result = run_check(self.base, self.root / 'base/evidence.json', self.work / 'bare.result.json', '-S')
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertTrue(result['checks'][0]['measured']['O0.schema']['value'].startswith('unavailable'))

    def test_each_mutant_fails_exactly_its_rules(self):
        for name, (_, rules) in sorted(make.MUTANTS.items()):
            with self.subTest(mutant=name):
                folder = next((self.root / name).glob('Recording-*'))
                process, result = run_check(folder, self.root / name / 'evidence.json', self.work / f'{name}.result.json')
                self.assertEqual(process.returncode, 1, process.stderr)
                ids, failing = failed(result)
                self.assertEqual(sorted(failing), sorted(rules))
                self.assertEqual(ids, sorted({rule.split('.')[0] for rule in rules}))

    def test_without_jsonschema_the_stdlib_rules_still_fail(self):
        # macOS /usr/bin/python3 has no jsonschema: O0.schema passes as unavailable, the rest stays.
        for name, (_, rules) in sorted(make.MUTANTS.items()):
            if 'O0.schema' in rules:
                with self.subTest(mutant=name):
                    folder = next((self.root / name).glob('Recording-*'))
                    process, result = run_check(folder, self.root / name / 'evidence.json',
                                                self.work / f'{name}.bare.result.json', '-S')
                    self.assertEqual(process.returncode, 1, process.stderr)
                    self.assertEqual(sorted(failed(result)[1]), sorted(set(rules) - {'O0.schema'}))

    def test_variants_pass_and_compare_equal_to_the_base(self):
        base = oracle.check(self.base, oracle.load_evidence(self.root / 'base/evidence.json'))
        for name in sorted(make.VARIANTS):
            with self.subTest(variant=name):
                folder = next((self.root / name).glob('Recording-*'))
                process, result = run_check(folder, self.root / name / 'evidence.json', self.work / f'{name}.result.json')
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertEqual(failed(result), ([], []))
                code, report = oracle.compare(base, result)
                self.assertEqual(code, 0, report)

    def test_every_rule_is_shown_failing(self):
        # A rule no mutant or evidence case fails could be an always-pass and nothing would notice.
        _, result = run_check(self.base, self.root / 'base/evidence.json', self.work / 'rules.result.json')
        rules = {rule for check in result['checks'] for rule in check['measured']}
        shown = {rule for _, failing in make.MUTANTS.values() for rule in failing}
        shown |= {rule for _, failing in EVIDENCE_CASES for rule in failing}
        self.assertEqual(sorted(rules - shown), [])
        self.assertEqual(sorted(shown - rules), [])

    def test_mutants_keep_the_folder_valid_where_they_do_not_target_o0(self):
        for name, (_, rules) in make.MUTANTS.items():
            if not {'O0.schema', 'O0.bounds'} & set(rules):
                with self.subTest(mutant=name):
                    events.validate(json.loads(next((self.root / name).glob('Recording-*/session.json')).read_text()))

    def evidence_case(self, change):
        folder, evidence = self.copy()
        value = json.loads(evidence.read_text())
        change(value)
        evidence.write_text(json.dumps(value))
        return run_check(folder, evidence, evidence.with_name('result.json'))

    def test_evidence_facts_fail_only_their_rules(self):
        for index, (change, rules) in enumerate(EVIDENCE_CASES):
            with self.subTest(case=index, rules=rules):
                process, result = self.evidence_case(change)
                self.assertEqual(process.returncode, 1, process.stderr)
                self.assertEqual(sorted(failed(result)[1]), sorted(rules))

    def test_unusable_evidence_is_refused(self):
        for change in (lambda e: e['actions'].pop('kill'), lambda e: e.update(protocolCommit='abc'),
                       lambda e: e['actions']['mute'].update(observedAtMs=0), lambda e: e.pop('nativeTones'),
                       # The resume requests and the identity they must name are required facts, and
                       # only the driver's own copy of the control pipe counts as observing a request.
                       lambda e: e.pop('resumeRequests'), lambda e: e.pop('captureId'),
                       lambda e: e.update(timelineOriginMs=1793880000000.5),
                       lambda e: e['resumeRequests'][0].update(via='app-log'),
                       lambda e: e['resumeRequests'][0].pop('captureId'),
                       lambda e: e['resumeRequests'][0].pop('connection'),
                       lambda e: e['resumeRequests'][0].update(connection=0),
                       lambda e: e['resumeRequests'][0].update(connection=True),
                       lambda e: e['resumeRequests'][0].update(connection=1.0),
                       lambda e: e['resumeRequests'][0].update(delivered='no'),
                       lambda e: e['resumeRequests'][0].update(delivered=None),
                       lambda e: e['resumeRequests'][0].update(delivered=0)):
            process, result = self.evidence_case(change)
            self.assertEqual(process.returncode, 2)
            self.assertIn('unusable input: evidence', process.stderr)

    def test_resume_is_asked_once_per_control_connection(self):
        # D52.5: a new control connection (a new hello) discards the in-flight resume and asks once
        # more; a second ask on the same connection is the duplicate O3.resumeOnce exists to catch.
        def second(connection):
            return lambda e: e['resumeRequests'].append(dict(e['resumeRequests'][0], atMs=98410.25,
                                                             requestId='resume-2', connection=connection))
        for connection, rules in ((2, []), (1, ['O3.resumeOnce'])):
            with self.subTest(connection=connection):
                process, result = self.evidence_case(second(connection))
                self.assertEqual(failed(result)[1], rules, process.stdout)

    def test_an_undelivered_resume_request_still_counts(self):
        # scenario.json resumeObservation.fields.delivered: the relay's forward failed (the browser
        # had closed its end), but the host asked, so the request is evidence like any other.
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                process, result = self.evidence_case(lambda e: e['resumeRequests'][0].update(delivered=delivered))
                self.assertEqual((process.returncode, failed(result)[1]), (0, []), process.stdout + process.stderr)
        def extra(e):
            e['resumeRequests'].append(dict(e['resumeRequests'][0], atMs=98410.25, requestId='resume-2', delivered=False))
        process, result = self.evidence_case(extra)
        self.assertEqual(failed(result)[1], ['O3.resumeOnce'], process.stdout)
        fields = json.loads((LIVE / 'scenario.json').read_text())['evidence']['resumeObservation']['fields']
        self.assertIn('The request still counts', fields['delivered'])

    def test_rotated_wavs_are_read_through_spans(self):
        # GNOME's RotatingWAV keeps one logical track in several WAVs, listed in `spans`.
        folder, evidence = self.copy()
        document = json.loads((folder / 'session.json').read_text())
        track = next(t for t in document['tracks'] if t['id'] == 'span-b1')
        with wave.open(str(folder / track['file'])) as reader:
            data = reader.readframes(reader.getnframes())
        cut = 100000
        rows = []
        for index, (first, last) in enumerate(((0, cut), (cut, track['frames']))):
            name = f"track-b1-part{index}.wav"
            with wave.open(str(folder / name), 'wb') as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(track['sampleRate'])
                writer.writeframes(data[first * 4:last * 4])
            rows.append(dict(file=name, sampleRate=track['sampleRate'], channels=2, frames=last - first,
                             startOffsetMs=track['startOffsetMs'] + first * 1000 / track['sampleRate'], ended=True))
        (folder / track['file']).unlink()
        track.update(file=rows[0]['file'], spans=rows)
        (folder / 'session.json').write_text(json.dumps(document))
        process, result = run_check(folder, evidence, evidence.with_name('spans.result.json'))
        self.assertEqual(process.returncode, 0, process.stdout)
        # GNOME rounds a later piece's start to its 48 kHz grid: within one session sample passes.
        for shift, rules in ((1 / 48, []), (-1 / 48, []), (0.1, ['O0.pieces']), (-0.1, ['O0.pieces'])):
            with self.subTest(shiftMs=shift):
                rows[1]['startOffsetMs'] = track['startOffsetMs'] + cut * 1000 / track['sampleRate'] + shift
                (folder / 'session.json').write_text(json.dumps(document))
                process, result = run_check(folder, evidence, evidence.with_name('shift.result.json'))
                self.assertEqual(failed(result)[1], rules, process.stdout)
        rows[1]['startOffsetMs'] = track['startOffsetMs'] + cut * 1000 / track['sampleRate']
        rows[1]['frames'] += 1
        (folder / 'session.json').write_text(json.dumps(document))
        process, result = run_check(folder, evidence, evidence.with_name('bad.result.json'))
        self.assertEqual(failed(result), (['O0'], ['O0.files', 'O0.pieces']))

    def test_folder_and_id_are_compared_as_uuid_values(self):
        # SPEC says only Recording-<UUID>: Mac writes the folder lowercase and the id uppercase
        # (variant uuid-case). Another UUID, or a name or id that is not a canonical UUID, fails
        # O0.oneFolder alone and never aborts O0. -S: no jsonschema, so O0.schema stays out of it.
        for change, folder in ((dict(id='02000000-0000-4000-8000-00000000000A'), None),
                               (dict(id='not-a-uuid'), None), (dict(id=None), None),
                               ({}, 'Recording-not-a-uuid'),
                               ({}, 'Recording-02000000000040008000000000000002'),
                               ({}, 'Recording-{02000000-0000-4000-8000-000000000002}')):
            with self.subTest(change=change, folder=folder):
                path, evidence = self.copy()
                document = json.loads((path / 'session.json').read_text())
                document.update(change)
                (path / 'session.json').write_text(json.dumps(document))
                if folder:
                    path = path.rename(path.with_name(folder))
                    value = json.loads(evidence.read_text())
                    value['newFolders'] = [folder]
                    evidence.write_text(json.dumps(value))
                process, result = run_check(path, evidence, evidence.with_name('uuid.result.json'), '-S')
                self.assertEqual(process.returncode, 1, process.stderr)
                self.assertEqual(failed(result), (['O0'], ['O0.oneFolder']))
        folder = next((self.root / 'uuid-case').glob('Recording-*'))
        document = json.loads((folder / 'session.json').read_text())
        self.assertNotEqual(folder.name, f"Recording-{document['id']}")
        self.assertEqual(folder.name.lower(), f"Recording-{document['id']}".lower())
        self.assertRegex(document['id'], '[A-F]')

    def test_a_track_with_no_start_never_aborts_a_rule_group(self):
        # Mac commits a declaration made before its restore as an ended 0-frame track with no
        # start (SPEC: startOffsetMs is optional). Extents and presence are taken over placeable
        # tracks only, so O2, O3 and O6 still measure; O0.offsets and O4.outcomeRows stay strict.
        folder = next((self.root / 'pre-restore-declaration').glob('Recording-*'))
        document = json.loads((folder / 'session.json').read_text())
        events.validate(document)
        declared = [t for t in document['tracks'] if 'startOffsetMs' not in t]
        self.assertEqual([(t['frames'], t['ended'], t['sourceTrackId']) for t in declared],
                         [(0, True, make.A_KEY), (0, True, make.B_KEY)])
        self.assertNotIn('endOffsetMs', declared[0])
        process, result = run_check(folder, self.root / 'pre-restore-declaration/evidence.json',
                                    self.work / 'declared.result.json')
        rules = [rule for check in result['checks'] for rule in check['measured']]
        self.assertEqual([rule for rule in rules if rule.endswith('.error')], [])
        self.assertEqual(failed(result), (['O0', 'O4'], ['O0.offsets', 'O4.outcomeRows']))
        self.assertEqual(events.others_presence(document),
                         events.others_presence(json.loads((self.base / 'session.json').read_text())))
        # The same holds for a system declaration: O3.system counts placed system tracks only, so a
        # 0-frame system track with no start (missing or null) fails O0.offsets alone.
        for null in (False, True):
            with self.subTest(system_declaration=null and 'null start' or 'missing start'):
                folder, evidence = self.copy()
                document = json.loads((folder / 'session.json').read_text())
                system = next(t for t in document['tracks'] if t['kind'] == 'system')
                declared = {k: v for k, v in system.items() if k not in ('startOffsetMs', 'endOffsetMs', 'spans')}
                declared.update(id='declared-system', file='track-declared-system.wav', frames=0, ended=True)
                if null:
                    declared.update(startOffsetMs=None, endOffsetMs=None)
                with wave.open(str(folder / declared['file']), 'wb') as writer:
                    writer.setnchannels(declared['channels'])
                    writer.setsampwidth(2)
                    writer.setframerate(declared['sampleRate'])
                document['tracks'].append(declared)
                events.validate(document)
                (folder / 'session.json').write_text(json.dumps(document))
                process, result = run_check(folder, evidence, evidence.with_name('system.result.json'))
                rules = [rule for check in result['checks'] for rule in check['measured']]
                self.assertEqual([rule for rule in rules if rule.endswith('.error')], [])
                self.assertEqual(failed(result), (['O0'], ['O0.offsets']), process.stdout)
                base = oracle.check(self.base, oracle.load_evidence(self.root / 'base/evidence.json'))
                self.assertEqual(result['facts']['categorical']['spans'], base['facts']['categorical']['spans'])

    def test_compare_lines_up_two_results(self):
        def result(name):
            folder = next((self.root / name).glob('Recording-*'))
            return oracle.check(folder, oracle.load_evidence(self.root / name / 'evidence.json'))
        base = result('base')
        code, report = oracle.compare(base, copy.deepcopy(base))
        self.assertEqual(code, 0, report)
        code, report = oracle.compare(base, result('b-early'))
        self.assertEqual(code, 1)
        self.assertFalse(report['numeric']['bStartMs']['within'])
        self.assertTrue(report['categorical']['othersSource']['same'])
        code, report = oracle.compare(base, result('resumed-new-key'))
        self.assertEqual(code, 1)
        self.assertFalse(report['categorical']['remoteSources']['same'])
        # Mac's none/lost before system/lost is the same loss (SPEC S-6): same categorical facts.
        code, report = oracle.compare(base, result('mac-loss-form'))
        self.assertEqual(code, 0, report)
        self.assertTrue(report['categorical']['othersSource']['same'])
        code, report = oracle.compare(base, result('drop-restored'))
        self.assertFalse(report['categorical']['othersSource']['same'])
        later = copy.deepcopy(base)
        later['facts']['numeric']['restoredMs'] += 999
        later['facts']['numeric']['resumeLatencyMs'] += 999
        self.assertEqual(oracle.compare(base, later)[0], 0)
        later['facts']['numeric']['restoredMs'] += 2
        self.assertEqual(oracle.compare(base, later)[0], 1)
        for name in ('protocolCommit', 'extensionVersion'):
            other = dict(base, **{name: 'f' * 40})
            self.assertEqual(oracle.compare(base, other)[0], 2)

    def test_mute_waits_end_on_the_session_clock_too(self):
        # F(A) can lead the session clock (D61 output stamps), so a wait on F(A) alone could ask
        # for the mute before MUTE_START_MS or the unmute before mute + MUTE_MS on the session
        # clock; O2.muteStart and O2.duration hold the requests exactly (variant folder-leads).
        actions = json.loads((LIVE / 'scenario.json').read_text())['evidence']['actions']
        self.assertIn('once F(A) >= MUTE_START_MS and the session clock (times above) >= '
                      'MUTE_START_MS', actions['mute'])
        # The session clock is monotonic, with the wall-clock origin mapped once (Mac #148).
        times = json.loads((LIVE / 'scenario.json').read_text())['evidence']['times']
        self.assertIn('mapped onto the monotonic clock once', times)
        self.assertIn('the wall clock is never read again', times)
        self.assertIn('once F(A) >= mute + MUTE_MS and the session clock >= mute + MUTE_MS', actions['unmute'])
        session, tracks, evidence = make.run('folder-leads')
        mute, unmute = evidence['actions']['mute'], evidence['actions']['unmute']
        self.assertEqual([mute['requestedAtMs'], unmute['requestedAtMs'] - mute['requestedAtMs']],
                         [oracle.C['MUTE_START_MS'], oracle.C['MUTE_MS']])

    def test_resume_observation_keeps_the_host_the_browsers_child(self):
        # The Mac control host knows its browser only by getppid(): a relay that spawned it would
        # change the app under test. The host is exec'd by the launcher; only its stdout is relayed.
        observation = json.loads((LIVE / 'scenario.json').read_text())['evidence']['resumeObservation']
        self.assertIn("keeps the host the browser's direct child", observation['point'])
        self.assertIn('then execs the production host', observation['point'])
        self.assertIn('The browser-to-host direction is not touched', observation['point'])
        self.assertIn("checks that this pid's parent is the browser process and fails the run otherwise",
                      observation['process'])

    def test_corpus_constants_are_read_not_copied(self):
        scenario = json.loads((LIVE / 'scenario.json').read_text())
        corpus = json.loads((ROOT / 'conformance/multistream/corpus.json').read_text())['constants']
        names = scenario['corpus']['constants']
        self.assertEqual(set(names), {'LATE_REMOTE_MS', 'BLEED_DB', 'SPAN_RATIO_MIN', 'SPARSE_DENSITY_MAX'})
        self.assertFalse(set(names) & set(scenario['constants']))
        self.assertEqual({n: oracle.C[n] for n in names}, {n: corpus[n] for n in names})
        self.assertTrue((LIVE / scenario['corpus']['file']).resolve() == ROOT / 'conformance/multistream/corpus.json')

    def test_scenario_tones_clear_the_app_tones_and_their_low_harmonics(self):
        # 440/880 Hz peers, 660 Hz replacement and GNOME microphone, 1210 Hz: none within 100 Hz.
        constants = json.loads((LIVE / 'scenario.json').read_text())['constants']
        for tone in (constants['TONE_A_HZ'], constants['TONE_B_HZ']):
            for base in (440, 660, 880, 1210):
                for harmonic in range(1, 5):
                    self.assertGreaterEqual(abs(tone - base * harmonic), 100, (tone, base, harmonic))

    def test_goertzel_reads_a_full_scale_sine_as_a_quarter(self):
        table = make.table(1000, 32767)
        self.assertAlmostEqual(pcm.goertzel(list(table) * 8, make.RATE, 1000), 0.25, places=3)
        self.assertLess(pcm.goertzel(list(table) * 8, make.RATE, 2000), 1e-9)


if __name__ == '__main__':
    unittest.main()
