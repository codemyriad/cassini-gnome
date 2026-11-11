"""Independent D58 (R79) refusals oracle. Apps replay the literal vectors through their own
start path, writer and Details reader. This is not production code for either app.

Writer: replays abstract start/selection steps and returns the folder decision, the sides
that opened, the selection events, the stored `refusals` member, othersFrom,
state/problemKind and the start banner. Wire: turns an
ok:false control result into the browser refusal it records. Readers: turns a stored list
into the Audio sources lines and the entries Copy details keeps.
"""
import re

WORD = re.compile(r'[A-Za-z]{1,32}')  # always fullmatch: `$` also matches before a final newline
CAP = 16
MESSAGE = 200
MAX_MS = 86460000
BANNER = 'errors.recordFailed.line'
PROFILES = ('fallback', 'noFallback')
NATIVE = ('me', 'others')
SIDES = ('me', 'others', 'browser')


def word(value):
    return isinstance(value, str) and WORD.fullmatch(value) is not None


def cut(text):
    return text[:MESSAGE]  # Python strings index Unicode code points


def entry(step):
    """A stored entry has only the fields given, in this order."""
    item = {'atMs': step['atMs'], 'side': step['side'], 'code': step['code']}
    if word(step.get('detail')):
        item['detail'] = step['detail']
    if isinstance(step.get('message'), str) and step['message']:  # an empty message is omitted
        item['message'] = cut(step['message'])
    return item


def append(stored, item):
    if len(stored) < CAP:
        stored.append(item)
    else:
        stored[-1] = item  # keep the first 15 plus the latest


def _turned_on(steps, index):
    """The select a refused turn-on belongs to, or None for a start refusal."""
    step = steps[index]
    back = index - 1
    while back >= 0 and steps[back]['op'] == 'refuse' and steps[back]['atMs'] == step['atMs']:
        back -= 1  # several sides refused by the same select
    if back < 0 or steps[back]['op'] != 'select' or steps[back]['atMs'] != step['atMs']:
        return None
    return steps[back]


def _turn_on_side(select, side):
    return select.get('me') is True if side == 'me' else select.get('others') is True and side in ('others', 'browser')


def validate(case):
    request, steps = case['request'], case['steps']
    if case.get('profile') not in (None, *PROFILES):
        raise ValueError('unknown profile')
    if request.get('me') is not True or request.get('others') not in ('system', 'browser', False):
        raise ValueError('apps always start with me: true; others is system, browser or false')
    for index, step in enumerate(steps):
        if index and step['atMs'] < steps[index - 1]['atMs']:
            raise ValueError('steps are in time order')
        if step['op'] != 'refuse':
            continue
        select = _turned_on(steps, index)
        if select is not None and not _turn_on_side(select, step['side']):
            select = None
        if select is None and step['atMs'] != 0:
            raise ValueError('a refuse with atMs > 0 must follow the select that turns its side on')


def _start(case, profile):
    """Sides that open at 0, and where others come from."""
    request, steps = case['request'], case['steps']
    start = {s['side']: s for i, s in enumerate(steps) if s['op'] == 'refuse' and s['atMs'] == 0 and _turned_on(steps, i) is None}
    requested = (
        {'browser'}
        if request['others'] == 'browser'
        else ({'me'} | ({'others'} if request['others'] == 'system' else set()))
    )
    opened = requested - set(start)
    source = request['others'] or None
    browser = start.get('browser')
    fallback = browser is not None and (profile == 'fallback' and browser['code'] == 'startRejected' and browser.get('detail') != 'busy')
    # A start refusal names a side the app really tried to open on this profile: a
    # requested side, or on a fallback the system tap for The others.
    if set(start) - requested - ({'others'} if fallback else set()):
        raise ValueError('a start refusal names a side this profile never opens')
    if browser is not None:
        if fallback:
            source = 'system'
            if 'others' not in start:
                opened.add('others')
        else:
            opened = set()  # nothing records: a rejected Start without fallback, startFailed, or busy
    return opened, source


def replay(case, profile):
    validate(case)
    request, steps = case['request'], case['steps']
    opened, source = _start(case, profile)
    folder = bool(opened)
    selected = {'me': True, 'others': request['others'] is not False}
    # The start selection and othersSource record the request, not what opened.
    selection = [dict(atMs=0, me=True, others=selected['others'], reason='start')]
    others_from = source if selected['others'] else None
    stored, failed = [], False
    for index, step in enumerate(steps):
        op = step['op']
        if op == 'refuse':
            append(stored, entry(step))
        elif op == 'select':
            changed = False
            for side in NATIVE:
                if side not in step or step[side] == selected[side]:
                    continue
                refused = any(s['op'] == 'refuse' and _turned_on(steps, i) is step and _turn_on_side({side: True}, s['side'])
                              for i, s in enumerate(steps[index + 1:], index + 1))
                if refused:
                    continue  # a refused turn-on leaves the selection unchanged
                selected[side], changed = step[side], True
                if step[side]:
                    if side == 'me':
                        if request['others'] != 'browser':
                            opened.add('me')
                        elif source == 'browser':
                            opened.add('browser')
                    else:
                        opened.add('others' if source == 'system' else 'browser')
                if side == 'others':
                    others_from = source if step[side] else None
            if changed:
                selection.append(dict(atMs=step['atMs'], me=selected['me'], others=selected['others'], reason='user'))
        elif op == 'fail':
            failed = True  # a mid-call failure is never a refusal (P10)
        elif op == 'stop':
            break
        else:
            raise ValueError('unknown op ' + op)
    if not folder:
        return dict(folder=False, opened=[], selection=None, othersFrom=None, state=None, problemKind=None,
                    refusals=None, banner=dict(id=BANNER, refusals=stored))
    state = 'interrupted' if failed else 'completed'
    return dict(folder=True, opened=[side for side in SIDES if side in opened], selection=selection,
                othersFrom=others_from if selected['others'] else None, state=state,
                problemKind='capture' if failed else None, refusals=stored or None, banner=None)


def from_result(result):
    """The browser refusal an ok:false Start result records (atMs is the app's)."""
    item = {'side': 'browser', 'code': 'startRejected'}
    if word(result.get('code')):
        item['detail'] = result['code']
    if isinstance(result.get('error'), str) and result['error']:  # an empty error is omitted
        item['message'] = cut(result['error'])
    return item


SOURCES = {'me': 'details.source.microphone', 'others': 'details.source.system', 'browser': 'details.source.browser'}
ROLE = {'me': 'me', 'others': 'others', 'browser': 'others'}  # browser audio is The others' audio
REFUSED = ('permissionDenied', 'deviceUnavailable', 'startRejected')


def well_formed(item):
    """An entry the session schema accepts. A null detail or message means absent."""
    if not isinstance(item, dict):
        return False
    at = item.get('atMs')
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at <= MAX_MS:
        return False
    if not word(item.get('side')) or not word(item.get('code')):
        return False
    if item.get('detail') is not None and not word(item['detail']):
        return False
    message = item.get('message')
    return message is None or isinstance(message, str) and len(message) <= MESSAGE


def lines(stored, sources=(), not_selected=()):
    """Audio sources lines after the source rows, which render as today."""
    shown = []
    for item in stored if isinstance(stored, list) else []:  # a non-list member is ignored whole
        if not well_formed(item):
            continue  # a malformed entry is skipped
        if item['side'] not in SOURCES:
            continue  # an unknown side is not shown
        known = item['code'] in REFUSED  # anything else, startFailed included, is shown as failed
        line = {'id': 'details.sources.refused.' + item['code'] if known else 'details.sources.failed',
                'source': SOURCES[item['side']]}
        if line not in shown:  # one line per distinct line, in first-occurrence order
            shown.append(line)
    refused = {ROLE[item['side']] for item in copied(stored) if item['side'] in SOURCES}
    result = shown + [{'id': 'details.sources.notSelected', 'source': SOURCES[side]}
                      for side in not_selected if ROLE[side] not in refused]
    if not sources and not result:  # empty only when no other Audio sources line is shown
        result.append({'id': 'details.sources.empty'})
    return result


def copied(stored):
    """The entries Copy details lists: every well-formed one, in stored order, exactly as
    stored (no merging, no re-cut, no Unicode normalization). Malformed ones are left out."""
    return [item for item in stored if well_formed(item)] if isinstance(stored, list) else []
