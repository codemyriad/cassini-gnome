"""D58 (R79) classified refusals: literal vectors, oracle, mutation guards and schemas."""
import copy
import json
from pathlib import Path
import re
import unittest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / 'conformance/refusals/refusals.py'
WRITER = json.loads((ROOT / 'conformance/refusals/writer.json').read_text())
READERS = json.loads((ROOT / 'conformance/refusals/readers.json').read_text())
CODES = {'permissionDenied', 'deviceUnavailable', 'startRejected', 'startFailed'}
RESULT_CODES = ['busy', 'limit', 'noSources', 'gone', 'notWebPage', 'pageUnreachable', 'invalid', 'resumeRefused',
                'workletBlocked', 'audioBlocked']


def load(source=None):
    namespace = {'__name__': 'refusals'}
    exec(compile(source if source is not None else ORACLE.read_text(), str(ORACLE), 'exec'), namespace)
    return namespace


def mutant(old, new):
    source = ORACLE.read_text()
    assert source.count(old) == 1, old
    return load(source.replace(old, new))


def runs(case):
    return [case['profile']] if 'profile' in case else ['fallback', 'noFallback']


def failing(oracle):
    """Names of the writer, wire and reader cases this oracle gets wrong."""
    names = set()
    for case in WRITER['cases']:
        for profile in runs(case):
            try:
                actual = oracle['replay'](case, profile)
            except Exception:  # a mutant that rejects or crashes on a literal case fails it
                names.add(case['name'])
                continue
            if any(actual[key] != value for key, value in case['expected'].items()):
                names.add(case['name'])
    for case in WRITER['wire']:
        if oracle['from_result'](case['result']) != case['expected']:
            names.add(case['name'])
    for case in READERS['readers']:
        try:
            if oracle['lines'](case['stored'], case.get('sources', ()), case.get('notSelected', ())) != case['expectedLines']:
                names.add(case['name'])
            if 'expectedCopy' in case and oracle['copied'](case['stored']) != case['expectedCopy']:
                names.add(case['name'])
        except Exception:  # a mutant that crashes on a literal case fails it
            names.add(case['name'])
    return names


def stored_lists():
    for case in WRITER['cases']:
        expected = case['expected']
        if expected.get('refusals'):
            yield case['name'], expected['refusals']
        if expected.get('banner'):
            yield case['name'] + ' (banner)', expected['banner']['refusals']


GRAPHEMES = ("return ''.join(__import__('functools').reduce(lambda acc, c: acc[:-1] + [acc[-1] + c] "
             "if acc and __import__('unicodedata').combining(c) else acc + [c], text, [])[:MESSAGE])")


class RefusalTests(unittest.TestCase):
    def test_vectors_are_well_formed(self):
        self.assertEqual((WRITER['version'], READERS['version']), (1, 1))
        oracle = load()
        names = [c['name'] for c in WRITER['cases'] + WRITER['wire'] + READERS['readers']]
        self.assertEqual(len(names), len(set(names)))
        for case in WRITER['cases']:
            with self.subTest(case=case['name']):
                oracle['validate'](case)
                self.assertLessEqual(set(case['expected']), {'folder', 'opened', 'selection', 'othersFrom', 'state', 'problemKind', 'refusals', 'banner'})
                for step in case['steps']:
                    if step['op'] == 'refuse':
                        self.assertIn(step['code'], CODES)
                        self.assertIn(step['side'], {'me', 'others', 'browser'})
        for case in WRITER['wire']:
            self.assertEqual(case['result']['ok'], False)

    def test_invalid_cases_are_rejected(self):
        validate = load()['validate']
        stop = {'op': 'stop', 'atMs': 9000}
        for steps in ([{'op': 'refuse', 'atMs': 5000, 'side': 'me', 'code': 'deviceUnavailable'}, stop],
                      [{'op': 'select', 'atMs': 5000, 'others': True},
                       {'op': 'refuse', 'atMs': 5000, 'side': 'me', 'code': 'deviceUnavailable'}, stop],
                      [{'op': 'select', 'atMs': 4000, 'me': True},
                       {'op': 'refuse', 'atMs': 5000, 'side': 'me', 'code': 'deviceUnavailable'}, stop]):
            with self.assertRaises(ValueError):
                validate({'name': 'invalid', 'request': {'me': True, 'others': 'system'}, 'steps': steps})
        # A start refusal names a side the profile really tries to open: never the system tap
        # without a fallback, nor a side that was not requested.
        replay = load()['replay']
        rejected = {'op': 'refuse', 'atMs': 0, 'side': 'browser', 'code': 'startRejected'}
        tap = {'op': 'refuse', 'atMs': 0, 'side': 'others', 'code': 'deviceUnavailable'}
        for profile, steps, others in (('noFallback', [rejected, tap], 'browser'),
                                       ('fallback', [dict(rejected, code='startFailed'), tap], 'browser'),
                                       ('fallback', [dict(rejected, detail='busy'), tap], 'browser'),
                                       ('fallback', [rejected], 'system'), ('fallback', [tap], False)):
            with self.subTest(profile=profile, steps=steps, others=others), self.assertRaises(ValueError):
                replay({'name': 'invalid', 'request': {'me': True, 'others': others}, 'steps': steps}, profile)
        # Mac replays the browser cases that hold on both profiles (review of #35).
        for name in ('start-failed-timeout-never-falls-back', 'start-rejected-busy-no-fallback'):
            self.assertNotIn('profile', next(c for c in WRITER['cases'] if c['name'] == name))

    def test_oracle_reproduces_every_case(self):
        self.assertEqual(failing(load()), set())
        names = {c['name'] for c in WRITER['cases']}
        self.assertLessEqual({'mic-denied-others-keep-recording', 'nothing-opens-no-folder',
                              'start-rejected-falls-back-to-computer-sound', 'start-rejected-without-fallback-records-nothing',
                              'start-failed-timeout-never-falls-back', 'late-turn-on-refused-is-appended',
                              'mid-call-failure-is-not-a-refusal', 'host-setup-broken-is-not-a-refusal',
                              'start-rejected-busy-no-fallback', 'start-rejected-then-mic-refused',
                              'start-rejected-and-both-native-refused', 'cap-keeps-first-15-and-latest',
                              'message-cut-at-200-code-points', 'others-unavailable-me-keeps-recording',
                              'start-rejected-without-code-falls-back', 'empty-message-is-omitted',
                              'start-rejected-and-mic-refused-without-fallback'}, names)
        for case in WRITER['cases']:
            self.assertIn('opened', case['expected'], case['name'])
            self.assertIn('selection', case['expected'], case['name'])

    def test_literal_cap_and_message_cases(self):
        case = next(c for c in WRITER['cases'] if c['name'] == 'cap-keeps-first-15-and-latest')
        self.assertEqual(sum(s['op'] == 'refuse' for s in case['steps']), 20)
        self.assertEqual([r['atMs'] for r in case['expected']['refusals']], list(range(2000, 17000, 1000)) + [21000])
        case = next(c for c in WRITER['cases'] if c['name'] == 'message-cut-at-200-code-points')
        message = case['steps'][0]['message']
        self.assertEqual(message, 'a' * 198 + '\U0001F600éz')
        self.assertEqual(case['expected']['refusals'][0]['message'], 'a' * 198 + '\U0001F600e')

    def test_mutations_fail_the_vectors(self):
        mutants = {
            'a refusal written for a fail': (
                'failed = True  # a mid-call failure is never a refusal (P10)',
                "failed = True; append(stored, dict(atMs=step['atMs'], side=step['side'], code='deviceUnavailable'))",
                'mid-call-failure-is-not-a-refusal'),
            'a folder kept when nothing opened': ('folder = bool(opened)', 'folder = True', 'nothing-opens-no-folder'),
            'a refusal that changes state': (
                "state = 'interrupted' if failed else 'completed'", "state = 'interrupted' if failed or stored else 'completed'",
                'mic-denied-others-keep-recording'),
            'message dropped': ("item['message'] = cut(step['message'])", 'pass', 'message-cut-at-200-code-points'),
            'message not cut': ('return text[:MESSAGE]  # Python strings index Unicode code points', 'return text',
                                'message-cut-at-200-code-points'),
            'message cut at UTF-16 units': (
                'return text[:MESSAGE]  # Python strings index Unicode code points',
                "return text.encode('utf-16-le')[:2 * MESSAGE].decode('utf-16-le', 'ignore')", 'message-cut-at-200-code-points'),
            'message cut at UTF-8 bytes': (
                'return text[:MESSAGE]  # Python strings index Unicode code points',
                "return text.encode()[:MESSAGE].decode('utf-8', 'ignore')", 'message-cut-at-200-code-points'),
            'message cut at graphemes': (
                'return text[:MESSAGE]  # Python strings index Unicode code points', GRAPHEMES, 'message-cut-at-200-code-points'),
            'a Mac-profile fallback': ("profile == 'fallback' and ", '', 'start-rejected-without-fallback-records-nothing'),
            'a fallback after startFailed': ("browser['code'] == 'startRejected' and ", '', 'start-failed-timeout-never-falls-back'),
            'a fallback after startRejected/busy': (" and browser.get('detail') != 'busy'", '', 'start-rejected-busy-no-fallback'),
            'the cap dropping the latest entry': (
                'stored[-1] = item  # keep the first 15 plus the latest', 'pass', 'cap-keeps-first-15-and-latest'),
            'the cap keeping the last 16': (
                'stored[-1] = item  # keep the first 15 plus the latest', 'stored.pop(0); stored.append(item)',
                'cap-keeps-first-15-and-latest'),
            'no cap': ('if len(stored) < CAP:', 'if True:', 'cap-keeps-first-15-and-latest'),
            'notSelected printed next to a refusal line': (
                ' if ROLE[side] not in refused]', ']', 'refusal-replaces-not-selected-and-empty'),
            'empty printed next to a refusal line': (
                'if not sources and not result:', 'if not sources:', 'refusal-replaces-not-selected-and-empty'),
            # Beyond R79's list: the refused turn-on and wire rules.
            'a refused turn-on changes the selection': (
                '                    continue  # a refused turn-on leaves the selection unchanged', '                    pass',
                'late-turn-on-refused-is-appended'),
            'a malformed result code is kept': (
                "if word(result.get('code')):", "if isinstance(result.get('code'), str):", 'malformed-code-is-dropped'),
            'the pattern accepts a final newline': (
                'return isinstance(value, str) and WORD.fullmatch(value) is not None',
                "return isinstance(value, str) and re.match('^[A-Za-z]{1,32}$', value) is not None",
                'code-with-a-trailing-newline-is-dropped'),
            'an unknown side is shown': ('            continue  # an unknown side is not shown',
                                         "            item = dict(item, side='browser')", 'unknown-side-is-not-shown'),
            'an unknown code has no line': ("if known else 'details.sources.failed'", "if known else None",
                                            'unknown-code-uses-sources-failed'),
            # Review of #35 (59015e8).
            'the fallback drops me': ("                opened.add('others')\n",
                                      "                opened.add('others')\n            opened.discard('me')\n",
                                      'start-rejected-falls-back-to-computer-sound'),
            'a refused side still gets a track': ('    opened = requested - set(start)\n', '    opened = set(requested)\n',
                                                  'mic-denied-others-keep-recording'),
            'an others refusal ends everything': (
                '    opened = requested - set(start)\n', "    opened = requested - set(start)\n    if 'others' in start: opened = set()\n",
                'others-unavailable-me-keeps-recording'),
            'othersFrom null when others is refused': (
                "    source = request['others'] or None", "    source = None if 'others' in start else (request['others'] or None)",
                'others-unavailable-me-keeps-recording'),
            'the fallback needs a detail': (
                " and browser.get('detail') != 'busy')", " and browser.get('detail') not in (None, 'busy'))",
                'start-rejected-without-code-falls-back'),
            'an empty message is kept': (
                "if isinstance(step.get('message'), str) and step['message']:", "if isinstance(step.get('message'), str):",
                'empty-message-is-omitted'),
            'an empty error is kept': (
                "if isinstance(result.get('error'), str) and result['error']:", "if isinstance(result.get('error'), str):",
                'empty-error-is-omitted'),
            'a refused me turn-on changes the selection': (
                '                    continue  # a refused turn-on leaves the selection unchanged',
                "                    if side == 'others': continue", 'cap-keeps-first-15-and-latest'),
            'a refused turn-on writes a selection event': (
                '            if changed:\n', '            if True:\n', 'late-turn-on-refused-is-appended'),
            'the reader keeps an entry without a valid atMs': (
                'if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at <= MAX_MS:', 'if False:',
                'an-entry-without-a-valid-atMs-is-skipped'),
            'the reader only checks that side and code are strings': (
                "if not word(item.get('side')) or not word(item.get('code')):",
                "if not isinstance(item.get('side'), str) or not isinstance(item.get('code'), str):",
                'an-entry-with-a-pattern-invalid-field-is-skipped'),
            'the reader ignores detail and message': (
                "    return message is None or isinstance(message, str) and len(message) <= MESSAGE", '    return True',
                'an-entry-with-a-pattern-invalid-field-is-skipped'),
            'the reader treats a null detail as malformed': (
                "if item.get('detail') is not None and not word(item['detail']):", "if 'detail' in item and not word(item['detail']):",
                'null-detail-and-message-are-absent'),
            'identical lines are repeated': ('        if line not in shown:  # one line per distinct line, in first-occurrence order',
                                             '        if True:', 'identical-lines-are-shown-once'),
            # Mac review of #35: the cut around 200 code points, as read back.
            'the reader counts UTF-16 units': (
                'and len(message) <= MESSAGE', "and len(message.encode('utf-16-le')) // 2 <= MESSAGE",
                'copy-details-keeps-messages-exactly-as-stored'),
            'the reader counts UTF-8 bytes': (
                'and len(message) <= MESSAGE', 'and len(message.encode()) <= MESSAGE', 'copy-details-keeps-messages-exactly-as-stored'),
            'the reader counts graphemes': (
                'and len(message) <= MESSAGE',
                "and len([c for c in message if not __import__('unicodedata').combining(c)]) <= MESSAGE",
                'copy-details-keeps-messages-exactly-as-stored'),
            'Copy details normalizes to NFC': (
                'return [item for item in stored if well_formed(item)] if isinstance(stored, list) else []',
                "return [dict(item, message=__import__('unicodedata').normalize('NFC', item['message'])) if isinstance(item.get('message'), str) "
                "else item for item in stored if well_formed(item)] if isinstance(stored, list) else []",
                'copy-details-keeps-messages-exactly-as-stored'),
            'Copy details re-cuts at UTF-16 units': (
                'return [item for item in stored if well_formed(item)] if isinstance(stored, list) else []',
                "return [dict(item, message=item['message'].encode('utf-16-le')[:2 * MESSAGE].decode('utf-16-le', 'ignore')) "
                "if isinstance(item.get('message'), str) else item for item in stored if well_formed(item)] if isinstance(stored, list) else []",
                'copy-details-keeps-messages-exactly-as-stored'),
            'Copy details keeps a malformed entry': (
                'return [item for item in stored if well_formed(item)] if isinstance(stored, list) else []',
                'return list(stored) if isinstance(stored, list) else []', 'copy-details-keeps-messages-exactly-as-stored'),
            # Mac review of #35: two reader rules (flagged for the coordinator).
            'a browser refusal keeps notSelected for The others': (
                "ROLE = {'me': 'me', 'others': 'others', 'browser': 'others'}",
                "ROLE = {'me': 'me', 'others': 'others', 'browser': 'browser'}",
                'browser-refusal-replaces-not-selected-for-the-others'),
            'empty printed next to notSelected lines': (
                'if not sources and not result:', 'if not sources and not shown:', 'not-selected-alone-hides-empty'),
            'a stored startFailed gets a refused line': (
                "REFUSED = ('permissionDenied', 'deviceUnavailable', 'startRejected')",
                "REFUSED = ('permissionDenied', 'deviceUnavailable', 'startRejected', 'startFailed')",
                'a-stored-start-failed-uses-sources-failed'),
        }
        for name, (old, new, case) in mutants.items():
            with self.subTest(mutant=name):
                self.assertIn(case, failing(mutant(old, new)))

    def test_expected_lists_validate_against_the_session_schema(self):
        schema = json.loads((ROOT / 'schemas/session.schema.json').read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        base = json.loads((ROOT / 'conformance/examples/session.json').read_text())
        for name, refusals in stored_lists():
            with self.subTest(case=name):
                validator.validate(dict(copy.deepcopy(base), refusals=refusals))
        valid = lambda refusals: validator.is_valid(dict(copy.deepcopy(base), refusals=refusals))
        item = {'atMs': 0, 'side': 'me', 'code': 'deviceUnavailable'}
        self.assertTrue(valid(None))
        self.assertTrue(valid([item] * 16))
        self.assertFalse(valid([item] * 17))
        self.assertTrue(valid([dict(item, message='\U0001F600' * 200)]))  # code points, not UTF-16 units or bytes
        self.assertFalse(valid([dict(item, message='a' * 201)]))
        self.assertTrue(valid([dict(item, atMs=86460000, detail='a' * 32, futureMember=1)]))
        for bad in (dict(item, atMs=-1), dict(item, atMs=86460001), dict(item, detail='not a code!'), dict(item, detail='a' * 33),
                    dict(item, code=''), dict(item, side='me too'), {'atMs': 0, 'side': 'me'}, dict(item, message=7)):
            with self.subTest(bad=bad):
                self.assertFalse(valid([bad]))
        self.assertTrue(valid([dict(item, detail=None, message=None)]))  # S-6: null means absent
        # The reader's malformed rule is the item schema (a final newline aside: see SPEC).
        well_formed = load()['well_formed']
        for case in READERS['readers']:
            for entry in case['stored'] if isinstance(case['stored'], list) else []:
                if isinstance(entry, dict) and any(isinstance(v, str) and v.endswith('\n') for v in entry.values()):
                    continue
                with self.subTest(reader=case['name'], entry=entry):
                    self.assertEqual(well_formed(entry), valid([entry]))
        # Expected selection lists are valid session selection events.
        for case in WRITER['cases']:
            if case['expected'].get('selection'):
                events = [dict(type='selection', **event) for event in case['expected']['selection']]
                with self.subTest(selection=case['name']):
                    validator.validate(dict(copy.deepcopy(base), events=events))
        # Refusals never change the session's existing rules.
        self.assertFalse(validator.is_valid(dict(copy.deepcopy(base), state='completed', problem='refused', refusals=[item])))

    def test_result_code_schema_and_wire_cases(self):
        schema = json.loads((ROOT / 'schemas/control-extension.schema.json').read_text())
        validator = Draft202012Validator(schema)
        for case in WRITER['wire']:
            code = case['result'].get('code')
            pattern_valid = isinstance(code, str) and re.fullmatch('[A-Za-z]{1,32}', code) is not None
            if case['name'] == 'code-with-a-trailing-newline-is-dropped':
                continue  # Python's jsonschema uses re.search, whose `$` accepts it; hosts match the whole string.
            with self.subTest(case=case['name']):
                # Hosts accept every one of these results; only pattern-valid codes are schema-valid.
                self.assertEqual(validator.is_valid(case['result']), code is None or pattern_valid)
        comment = next(b for b in schema['oneOf'] if b['properties']['type'].get('const') == 'result')['properties']['code']['$comment']
        self.assertIn(', '.join(RESULT_CODES) + '.', comment)

    def test_spec_and_readme_are_normative(self):
        spec, control = (ROOT / 'SPEC.md').read_text(), (ROOT / 'SPEC-CONTROL.md').read_text()
        readme = (ROOT / 'conformance/refusals/README.md').read_text()
        for text in (spec, readme):
            self.assertIn('first 15', text)
        self.assertIn('conformance/refusals/', spec)
        self.assertIn('first buffer', spec)
        for code in RESULT_CODES:
            self.assertIn('`' + code + '`', control)
        self.assertIn('response.schema.json', control)
        manifest = json.loads((ROOT / 'extension/manifest.json').read_text())
        self.assertIn('extension ' + manifest['version'], control)


if __name__ == '__main__':
    unittest.main()
