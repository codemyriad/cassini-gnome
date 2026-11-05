#!/usr/bin/env python3
"""Independent oracle for shared test 2, the live folder (D68).

    check.py check FOLDER --evidence EVIDENCE.json --out RESULT.json
    check.py compare GNOME.json MAC.json

`check` reads one Recording-<id>/ folder that an app wrote while the shared
driver ran the scenario (scenario.json), plus the driver's evidence.json, and
prints one JSON line per check {id, pass, measured}. It exits 1 when any check
fails and 2 when its input is unusable. Its only truth sources are the evidence
brackets, the scenario constants (and the corpus constants scenario.json names,
read from corpus.json, never copied) and PCM the oracle measures itself. App
readers are never consulted. Stdlib only, Python 3.9+, runs under `python3 -I`;
the JSON Schema step runs only where jsonschema is importable, and the result
says which (O0.schema).

`compare` refuses (exit 2) unless both results name the same protocol commit
and extension version; categorical facts must be identical and numeric facts
within COMPARE_BAND_MS, else exit 1.
"""
import argparse
from array import array
import importlib.util
import json
import math
from operator import mul
import os
from pathlib import Path
import re
import stat
import sys
import uuid
import wave

HERE = Path(__file__).resolve().parent


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


events = _module('live_folder_session_events', HERE.parent / 'check-session-events.py')
gaps = _module('live_folder_gaps', HERE.parent / 'gaps/gaps.py')
pcm = _module('live_folder_pcm', HERE.parent / 'pcm.py')

SCENARIO = json.loads((HERE / 'scenario.json').read_text())
C = dict(SCENARIO['constants'])
_corpus = json.loads((HERE / SCENARIO['corpus']['file']).read_text())['constants']
C.update({name: _corpus[name] for name in SCENARIO['corpus']['constants']})
ACTIONS = tuple(SCENARIO['evidence']['actions'])
COMPARE_BAND_MS = 1000
SAMPLE_MS = 1 / 16  # one 16 kHz session sample (D55)
FLOOR_DBFS = -200.0  # digital silence, reported as a finite number
ME = ('microphone', 'local')
NATIVE = ('microphone', 'local', 'system')
WAV_NAME = re.compile(r'track-[^/\\]+\.wav')
COMMIT = re.compile(r'[0-9a-f]{40}')
UUID_TEXT = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
VIA = SCENARIO['evidence']['resumeObservation']['via']


class Unusable(Exception):
    """The folder or evidence cannot be checked at all (exit 2)."""


def _strict(text, what):
    def reject(value):
        raise Unusable(f'{what}: non-finite number {value}')
    try:
        return json.loads(text, parse_constant=reject)
    except ValueError as error:
        raise Unusable(f'{what}: {error}') from None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_evidence(path):
    value = _strict(Path(path).read_text(encoding='utf-8'), 'evidence')
    problems = []
    if not isinstance(value, dict):
        raise Unusable('evidence: not an object')
    if value.get('scenario') != 'live-folder/1':
        problems.append('scenario')
    if not isinstance(value.get('app'), str) or not re.fullmatch(r'[a-z]{1,32}', value['app']):
        problems.append('app')
    if not isinstance(value.get('protocolCommit'), str) or not COMMIT.fullmatch(value['protocolCommit']):
        problems.append('protocolCommit')
    if not isinstance(value.get('extensionVersion'), str) or not value['extensionVersion']:
        problems.append('extensionVersion')
    folders = value.get('newFolders')
    if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
        problems.append('newFolders')
    actions = value.get('actions')
    if not isinstance(actions, dict):
        problems.append('actions')
    else:
        for name in ACTIONS:
            bracket = actions.get(name)
            if not (isinstance(bracket, dict) and _number(bracket.get('requestedAtMs')) and
                    _number(bracket.get('observedAtMs')) and bracket['requestedAtMs'] <= bracket['observedAtMs']):
                problems.append('actions.' + name)
    slot = value.get('slotC')
    if not (isinstance(slot, dict) and isinstance(slot.get('connectionState'), str) and
            type(slot.get('packetsReceived')) is int):
        problems.append('slotC')
    if type(value.get('maxSourceCount')) is not int:
        problems.append('maxSourceCount')
    tones = value.get('nativeTones')
    if not (isinstance(tones, dict) and _number(tones.get('microphone')) and 20 <= tones['microphone'] <= 20000):
        problems.append('nativeTones.microphone')
    if not isinstance(value.get('captureId'), str) or not value['captureId']:
        problems.append('captureId')
    if type(value.get('timelineOriginMs')) is not int or value['timelineOriginMs'] < 0:
        problems.append('timelineOriginMs')
    requests = value.get('resumeRequests')
    if not isinstance(requests, list):
        problems.append('resumeRequests')
    else:
        for index, request in enumerate(requests):
            if not (isinstance(request, dict) and _number(request.get('atMs')) and request.get('via') == VIA and
                    type(request.get('connection')) is int and request['connection'] >= 1 and
                    {'requestId', 'captureId', 'timelineOriginMs'} <= set(request) and
                    type(request.get('delivered', True)) is bool):
                problems.append(f'resumeRequests[{index}]')
    if problems:
        raise Unusable('evidence: missing or invalid ' + ', '.join(problems))
    return value


def pieces(track):
    """A track's physical WAVs: GNOME's RotatingWAV `spans` rows when present, else the track."""
    return track.get('spans') or [track]


def piece_problems(track):
    """Why a rotated track's `spans` rows do not tile its logical extent, else [].

    GNOME's RotatingWAV keeps one logical track in several WAVs: the first row copies the
    track's startOffsetMs, every later one starts where the frames before it end, rounded
    to its 48 kHz grid (rotating_wav.py), and padding for a gap is written into the piece
    (gapFrames, gapMaterialized), so pieces never leave space between them. Each row's
    stored start must be within one 16 kHz session sample of track start + frames before
    it / rate, in list order; the last must end at the track's end. That is placement,
    order, contiguity, coverage and no overlap at once; each piece's extent then starts at
    its own D55 placement, as everywhere (Folder.window)."""
    rows = track.get('spans')
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return ['spans is not a non-empty list of rows']
    if not _placeable(track):
        return ['the track has no known start, frames and rate']
    start, rate, before, problems = track['startOffsetMs'], track['sampleRate'], 0, []
    for index, row in enumerate(rows):
        if not (_number(row.get('startOffsetMs')) and type(row.get('frames')) is int and row['frames'] >= 0):
            return problems + [f'piece {index}: no known start and frames']
        if (row.get('sampleRate'), row.get('channels')) != (rate, track.get('channels')):
            problems.append(f'piece {index}: rate or channels differ from the track')
        want = start + before * 1000 / rate
        delta = row['startOffsetMs'] - want
        if abs(delta) > SAMPLE_MS + 1e-9:
            what = 'starts late, leaving a hole' if delta > 0 else 'starts early, overlapping'
            problems.append(dict(piece=index, file=row.get('file'), startOffsetMs=row['startOffsetMs'],
                                 expectedMs=want, deltaMs=round(delta, 4), problem=what))
        before += row['frames']
    end, logical = start + before * 1000 / rate, start + track['frames'] * 1000 / rate
    if abs(end - logical) > SAMPLE_MS + 1e-9:
        problems.append(dict(piecesEndMs=end, trackEndMs=logical, problem='the pieces do not end at the track end'))
    return problems


def uuid_value(text):
    """The UUID a canonical 8-4-4-4-12 hex string names, in either case, else None. SPEC says
    only Recording-<UUID>: Mac names its folder in lowercase and JSONEncoder writes the id in
    uppercase, so the folder and session.json's id are compared as values, never as bytes."""
    return uuid.UUID(text) if isinstance(text, str) and UUID_TEXT.fullmatch(text) else None


def extent(track):
    """[start, end) session ms: the D55 placement plus frames/rate (R88). Only for a placeable
    track: SPEC lets a track omit its start (a 0-frame declaration made before a restore), and
    such a track has no extent, so it never aborts a rule group; O0.offsets and O4.outcomeRows
    still hold it to the scenario."""
    start = events.placed_ms(track['startOffsetMs'])
    return start, start + track['frames'] * 1000 / track['sampleRate']


def at_event(start, at):
    """A span starts at an event's atMs (SPEC S-6): its extent is that time's D55 placement,
    within one session sample (a writer may start on its first native frame after atMs)."""
    return abs(start - events.placed_ms(at)) <= SAMPLE_MS + 1e-9


def dbfs(mean_square):
    return 10 * math.log10(mean_square / 32768 ** 2) if mean_square > 0 else FLOOR_DBFS


def uncovered(ranges, left, right):
    """Milliseconds of [left, right) outside the union of ranges."""
    covered, cursor = 0.0, left
    for a, b in sorted(ranges):
        a, b = max(a, cursor), min(b, right)
        if b > a:
            covered += b - a
            cursor = b
    return (right - left) - covered


class Folder:
    def __init__(self, root):
        self.root = Path(root)
        try:
            self.doc = _strict((self.root / 'session.json').read_text(encoding='utf-8'), 'session.json')
        except OSError as error:
            raise Unusable(f'session.json: {error}') from None
        if not isinstance(self.doc, dict) or not isinstance(self.doc.get('tracks'), list):
            raise Unusable('session.json: no tracks')
        self.tracks = [t for t in self.doc['tracks'] if isinstance(t, dict)]
        self._pcm = {}

    def path(self, name):
        """A WAV inside the folder: a safe basename, no symlink, a regular file."""
        if not isinstance(name, str) or not WAV_NAME.fullmatch(name):
            raise ValueError(f'unsafe WAV name {name!r}')
        path = self.root / name
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or path.resolve().parent != self.root.resolve():
            raise ValueError(f'{name}: not a regular file in the folder')
        return path

    def samples(self, name, channels):
        if name not in self._pcm:
            with wave.open(str(self.path(name))) as reader:
                data = array('h', reader.readframes(reader.getnframes()))
            if sys.byteorder != 'little':
                data.byteswap()
            self._pcm[name] = data
        return self._pcm[name]

    def window(self, tracks, left, right):
        """Interleaved samples of tracks over session ms [left, right), clipped to their pieces."""
        out, layout = array('h'), None
        for track in tracks:
            rate, channels = track['sampleRate'], track['channels']
            assert layout in (None, (rate, channels)), 'mixed rates or channel counts in one window'
            layout = rate, channels
            for piece in pieces(track):
                start = events.placed_ms(piece['startOffsetMs'])
                first = max(0, math.ceil((left - start) * rate / 1000))
                last = min(piece['frames'], math.ceil((right - start) * rate / 1000))
                if last > first:
                    out.extend(self.samples(piece['file'], channels)[first * channels:last * channels])
        return out, layout

    def level(self, tracks, left, right):
        """RMS dBFS over the window (None when it holds no samples)."""
        data, _ = self.window(tracks, left, right)
        return round(dbfs(sum(map(mul, data, data)) / len(data)), 2) if data else None

    def tone(self, tracks, left, right, frequency):
        """Peak level of a sine at `frequency` over the window, dBFS (None when empty)."""
        data, layout = self.window(tracks, left, right)
        if not data:
            return None
        rate, channels = layout
        power = sum(pcm.goertzel(data[c::channels], rate, frequency) for c in range(channels)) / channels
        return round(dbfs(4 * power * 32768 ** 2), 2)


def _placeable(track):
    return _number(track.get('startOffsetMs')) and type(track.get('frames')) is int and \
        type(track.get('sampleRate')) is int and track['sampleRate'] > 0


def _audible(level):
    return level is not None and level > C['SILENT_DBFS']


class Run:
    """One check run: the folder, the evidence and the roles the oracle derives from PCM."""

    def __init__(self, folder, evidence):
        self.f, self.e = folder, evidence
        self.doc, self.tracks = folder.doc, folder.tracks
        self.actions = evidence['actions']
        self.events = [e for e in self.doc.get('events') or [] if isinstance(e, dict)]
        self.browser = [t for t in self.tracks if t.get('kind') not in NATIVE]
        self.keys = {}
        for track in self.browser:
            if track.get('sourceTrackId') is not None and _placeable(track):
                self.keys.setdefault(track['sourceTrackId'], []).append(track)
        for spans in self.keys.values():
            spans.sort(key=lambda t: extent(t)[0])
        self.roles = {'A': [], 'B': []}
        for key, spans in sorted(self.keys.items(), key=lambda item: extent(item[1][0])[0]):
            try:
                role = self.classify(spans[0])
            except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error):
                role = None  # O0.files reports the unreadable WAV
            if role:
                self.roles[role].append(key)

    def classify(self, track):
        """A or B by the dominant scenario tone in a span's first PROOF_MS, else None."""
        start = extent(track)[0]
        a = self.f.tone([track], start, start + C['PROOF_MS'], C['TONE_A_HZ'])
        b = self.f.tone([track], start, start + C['PROOF_MS'], C['TONE_B_HZ'])
        if _audible(a) and a > b:
            return 'A'
        if _audible(b) and b > a:
            return 'B'
        return None

    def key(self, role):
        found = self.roles[role]
        assert found, f'no {role} source: no remote key whose first span carries tone {role}'
        return found[0]

    def bracket(self, name, value, low_slack=0):
        bracket = self.actions[name]
        return bracket['requestedAtMs'] - low_slack <= value <= bracket['observedAtMs'] + C['ACTION_SLACK_MS']

    def others_events(self):
        return [e for e in self.events if e.get('type') == 'othersSource']

    def selections(self):
        return [e for e in self.events if e.get('type') == 'selection']

    def matching(self, source, reason):
        return [e for e in self.others_events() if (e.get('source'), e.get('reason')) == (source, reason)]

    def others_form(self):
        """othersSource as [source, reason] pairs, with each none/lost that a system/lost directly
        follows folded into it: SPEC S-6's two forms of one committed loss (Mac writes none/lost at
        the transport loss; GNOME keeps its written source until the fallback commits)."""
        pairs = [[e.get('source'), e.get('reason')] for e in self.others_events()]
        return [p for p, n in zip(pairs, pairs[1:] + [None]) if not (p == ['none', 'lost'] and n == ['system', 'lost'])]

    def lost(self):
        """The system/lost inside the kill bracket, when exactly one is."""
        found = [e for e in self.matching('system', 'lost') if self.bracket('kill', e['atMs'])]
        return found[0] if len(found) == 1 else None

    def restored(self):
        lost = self.lost()
        if lost is None:
            return None
        found = [e for e in self.matching('browser', 'restored') if e['atMs'] >= lost['atMs']]
        return found[0] if len(found) == 1 else None

    def resume_requests(self):
        """(every observed resume request, those naming this recording): its captureId and
        timelineOriginMs as the extension holds them, the identity resumeCapture names (SPEC-CONTROL)."""
        every = self.e['resumeRequests']
        identity = [self.e['captureId'], self.e['timelineOriginMs']]
        return every, [r for r in every if [r['captureId'], r['timelineOriginMs']] == identity]

    def resume_seam(self):
        """[committed loss, restoration]: the first browser/restored and the last system/lost
        written before it (a none/lost is not the commit: SPEC-CONTROL D52.5), else None."""
        written = [e for e in self.others_events() if _number(e.get('atMs'))]
        pairs = [(e.get('source'), e.get('reason')) for e in written]
        if ('browser', 'restored') not in pairs:
            return None
        restored = pairs.index(('browser', 'restored'))
        lost = [e['atMs'] for e, p in zip(written[:restored], pairs) if p == ('system', 'lost')]
        return [lost[-1], written[restored]['atMs']] if lost else None

    def last_frame(self):
        """The end of the last placed frame in the folder, or None when no track is placeable."""
        return max((extent(t)[1] for t in self.tracks if _placeable(t)), default=None)

    def post_kill(self, key):
        return [t for t in self.keys[key] if extent(t)[0] >= self.actions['kill']['requestedAtMs']]

    # O0 structure ------------------------------------------------------------------------
    def o0(self, rule):
        try:
            validator = events.schema()
        except ImportError:
            rule('schema', True, 'unavailable: jsonschema is not importable here')
        else:
            problems = sorted(error.message for error in validator.iter_errors(self.doc))
            rule('schema', not problems, problems[:5] or 'validated')
        try:
            events.bounds(self.doc)
            rule('bounds', True, 'S-6 bounds hold')
        except (ValueError, KeyError, TypeError) as error:
            rule('bounds', False, str(error))
        problems = []
        for track in self.tracks:
            total = 0
            for piece in pieces(track):
                try:
                    path = self.f.path(piece.get('file'))
                    with wave.open(str(path)) as reader:
                        shape = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(),
                                 reader.getcomptype(), reader.getnframes())
                    want = (track['channels'], 2, track['sampleRate'], 'NONE', piece['frames'])
                    if shape != want:
                        problems.append(f"{piece['file']}: {shape} != {want}")
                    if path.stat().st_size != 44 + piece['frames'] * track['channels'] * 2:
                        problems.append(f"{piece['file']}: not a 44-byte header plus exactly frames of PCM16")
                    total += piece['frames']
                except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error) as error:
                    problems.append(f"{track.get('id')}: {error}")
            if total != track.get('frames'):
                problems.append(f"{track.get('id')}: pieces hold {total} frames, the track says {track.get('frames')}")
        rule('files', not problems, problems[:5] or f'{len(self.tracks)} tracks, every WAV and span reachable')
        rows = {t.get('id'): piece_problems(t) for t in self.tracks if 'spans' in t}
        wrong = {str(k): v for k, v in rows.items() if v}
        rule('pieces', not wrong, dict(wrong=wrong) if wrong else
             f'{len(rows)} rotated tracks, every piece placed where the frames before it end')
        ids = [t.get('id') for t in self.tracks]
        owner, shared = {}, []
        for track in self.tracks:
            names = [p.get('file') for p in pieces(track)]
            if track.get('spans'):
                names = sorted(set(names) | {track.get('file')})  # the track names its current spans row
            if len(names) != len(set(names)):
                shared.append(track.get('id'))
            for name in names:
                if owner.setdefault(name, track.get('id')) != track.get('id'):
                    shared.append(name)
        repeated = sorted({i for i in ids if ids.count(i) > 1}, key=str)
        rule('unique', not repeated and not shared, dict(trackIds=repeated, wavs=shared) if repeated or shared
             else 'every track id and WAV is its own (SPEC S-6)')
        open_tracks = [t.get('id') for t in self.tracks if t.get('ended') is not True]
        rule('ended', not open_tracks, open_tracks or 'every track ended')
        wrong = []
        for track in self.tracks:
            start, end = track.get('startOffsetMs'), track.get('endOffsetMs')
            if not _number(start) or not _number(end) or \
                    abs(end - (start + track['frames'] / track['sampleRate'] * 1000)) > 1e-3:
                wrong.append(track.get('id'))
        rule('offsets', not wrong, wrong or 'every start known, end = start + frames/rate')
        folders = self.e['newFolders']
        name = self.f.root.resolve().name
        named = uuid_value(name[len('Recording-'):]) if name.startswith('Recording-') else None
        written = uuid_value(self.doc.get('id'))
        same = named == written if None not in (named, written) else 'malformed: needs Recording-<UUID> and a UUID id'
        rule('oneFolder', folders == [name] and same is True,
             dict(newFolders=folders, folder=name, id=self.doc.get('id'), sameUuid=same))
        kinds = sorted({t.get('kind') for t in self.tracks})
        rule('kinds', set(kinds) <= {'remote', 'microphone', 'system'}, kinds)
        state = [self.doc.get('state'), self.doc.get('problem'), self.doc.get('problemKind')]
        rule('state', state == ['completed', None, None], state)
        last = self.events[-1] if self.events else None
        rule('end', bool(last) and last.get('type') == 'end' and last.get('reason') == 'user', last)

    # O1 late remote -----------------------------------------------------------------------
    def o1(self, rule):
        rule('oneKey', len(self.roles['B']) == 1, self.roles['B'])
        key = self.key('B')
        start = extent(self.keys[key][0])[0]
        rule('late', start >= C['LATE_REMOTE_MS'], start)
        rule('bracket', self.bracket('lateRemote', start, C['ACTION_SLACK_MS']),
             dict(startMs=start, lateRemote=self.actions['lateRemote']))
        first = self.keys[key][:1]
        b = self.f.tone(first, start, start + C['PROOF_MS'], C['TONE_B_HZ'])
        a = self.f.tone(first, start, start + C['PROOF_MS'], C['TONE_A_HZ'])
        rule('bleed', a is not None and b is not None and a - b <= C['BLEED_DB'], dict(toneA=a, toneB=b))

    # O2 60 s mute ---------------------------------------------------------------------------
    def o2(self, rule):
        # The window itself comes from the evidence, so it is held to the scenario first. F(A)
        # can lead the session clock (the extension stamps a frame when it reaches the output,
        # D61, and GNOME checkpoints right after an append), so the driver waits on both: the
        # mute once F(A) and the session clock reach MUTE_START_MS, the unmute once both reach
        # mute.requested + MUTE_MS. The requests are session-clock times, so both lower bounds
        # hold by construction, exactly.
        mute, unmute, wait = self.actions['mute'], self.actions['unmute'], SCENARIO['driver']['boundsMs']
        latest = self.actions['meOff']['observedAtMs'] + C['MUTE_START_MS'] + wait['record'] + C['ACTION_SLACK_MS']
        rule('muteStart', C['MUTE_START_MS'] <= mute['requestedAtMs'] <= latest,
             dict(muteRequestedMs=mute['requestedAtMs'], boundsMs=[C['MUTE_START_MS'], latest]))
        held = unmute['requestedAtMs'] - mute['requestedAtMs']
        longest = mute['observedAtMs'] + wait['muteHold'] + C['ACTION_SLACK_MS'] - mute['requestedAtMs']
        rule('duration', C['MUTE_MS'] <= held <= longest, dict(heldMs=held, boundsMs=[C['MUTE_MS'], longest]))
        key = self.key('A')
        spans = self.keys[key]
        left, right = mute['requestedAtMs'], unmute['requestedAtMs']
        covering = [t['id'] for t in spans if extent(t)[0] <= left and extent(t)[1] >= right]
        touching = [t['id'] for t in spans if extent(t)[0] < right and extent(t)[1] > left]
        rule('oneSpan', len(covering) == 1 and touching == covering,
             dict(windowMs=[left, right], covering=covering, touching=touching))
        inside, overflow = [], False
        for track in spans:
            for gap in track.get('gaps') or []:
                if gap.get('kind') == 'overflow':
                    overflow |= gap['atMs'] < right
                elif gap['atMs'] < right and gap['atMs'] + gap.get('durationMs', 0) > left:
                    inside.append(gap)
        total = sum(g.get('durationMs', 0) for g in inside)
        longest = max([g.get('durationMs', 0) for g in inside] or [0])
        rule('gaps', not overflow and longest < 1000 and total <= C['MUTE_JITTER_MS'],
             dict(longestMs=longest, totalMs=total, overflow=overflow))
        silent = self.f.level(spans, left + C['EDGE_MS'], right - C['EDGE_MS'])
        rule('silent', silent is not None and silent <= C['SILENT_DBFS'], silent)
        before = self.f.tone(spans, left - 1000, left, C['TONE_A_HZ'])
        after = self.f.tone(spans, right + C['EDGE_MS'], right + C['EDGE_MS'] + 1000, C['TONE_A_HZ'])
        rule('toneEdges', _audible(before) and _audible(after), dict(before=before, after=after))
        seen = [e for e in self.others_events() if left <= e['atMs'] <= right]
        refusals = self.doc.get('refusals')
        seen += [r for r in refusals if isinstance(r, dict) and _number(r.get('atMs')) and
                 left <= r['atMs'] <= right] if isinstance(refusals, list) else []
        rule('noEvents', not seen, seen)
        others = [[a, b] for a, b in events.others_presence(self.doc)]
        own = [[a / 16, b / 16] for t in spans for a, b in gaps.presence(t)['present']]
        missing = dict(others=uncovered(others, left, right), source=uncovered(own, left, right))
        rule('present', max(missing.values()) <= C['MUTE_JITTER_MS'], missing)
        outcome = [s.get('captureOutcome') for s in self.sources() if s.get('sourceTrackId') == key]
        rule('outcome', outcome == ['captured'], outcome)

    def sources(self):
        outcome = self.doc.get('captureOutcome')
        rows = outcome.get('sources') if isinstance(outcome, dict) else None
        return [s for s in rows if isinstance(s, dict)] if isinstance(rows, list) else []

    # O3 forced resume -----------------------------------------------------------------------
    def o3(self, rule):
        sequence = self.others_form()
        none_lost = [e['atMs'] for e in self.matching('none', 'lost')]
        rule('sequence', sequence == [['browser', 'start'], ['system', 'lost'], ['browser', 'restored']] and
             all(self.bracket('kill', at, C['ACTION_SLACK_MS']) for at in none_lost),
             dict(folded=sequence, noneLostMs=none_lost))
        lost, restored = self.lost(), self.restored()
        rule('lost', lost is not None, dict(lost=[e['atMs'] for e in self.matching('system', 'lost')],
                                            kill=self.actions['kill']))
        ok = restored is not None and restored['atMs'] - lost['atMs'] <= C['RESUME_BOUND_MS'] and \
            restored['atMs'] <= self.actions['restore']['observedAtMs'] + C['ACTION_SLACK_MS']
        rule('restored', ok, dict(restored=[e['atMs'] for e in self.matching('browser', 'restored')],
                                  restore=self.actions['restore']))
        end = self.last_frame()
        if lost and restored and end is not None:
            states = [events.state_at(self.doc, at) for at in
                      ((lost['atMs'] + restored['atMs']) / 2, restored['atMs'], end)]
            sources = [s and s['source'] for s in states]
            rule('state', sources == ['system', 'browser', 'browser'], sources)
        else:
            rule('state', False, 'needs one lost in the kill bracket, one restored after it and a placed frame')
        a, b = self.key('A'), self.key('B')
        kill = self.actions['kill']['requestedAtMs']
        pre = {role: [t for t in self.keys[key] if extent(t)[0] < kill] for role, key in (('A', a), ('B', b))}
        counts = {role: [len(pre[role]), len(self.post_kill(key))] for role, key in (('A', a), ('B', b))}
        rule('spans', all(v == [1, 1] for v in counts.values()), dict(preAndPostKill=counts))
        # SPEC S-6: the Others are absent over the loss; the dead transport's audio is never zero-filled.
        ends = {}
        for role, spans in pre.items():
            present = [b / 16 for t in spans for _, b in gaps.presence(t)['present']]
            ends[role] = max(present) if present else None
        rule('lossAbsent', all(_number(v) and self.bracket('kill', v, C['ACTION_SLACK_MS']) for v in ends.values()),
             dict(lastPresentBeforeLossMs=ends, kill=self.actions['kill']))
        overlaps = []
        for key, spans in self.keys.items():
            for one, two in zip(spans, spans[1:]):
                if extent(two)[0] < extent(one)[1] - 1e-9:
                    overlaps.append([one['id'], two['id']])
        rule('noOverlap', not overlaps, overlaps)
        labels = {key: sorted({t.get('label') for t in spans}) for key, spans in self.keys.items()}
        rule('labels', all(len(v) == 1 for v in labels.values()), labels)
        starts = {role: [extent(t)[0] for t in self.post_kill(key)] for role, key in (('A', a), ('B', b))}
        rule('resumedStart', lost is not None and all(starts.values()) and
             all(s >= lost['atMs'] for v in starts.values() for s in v), starts)
        tones = {}
        for role, key in (('A', a), ('B', b)):
            hz = C['TONE_' + role + '_HZ']
            tones[role] = [self.f.tone([t], extent(t)[0], extent(t)[0] + C['PROOF_MS'], hz) for t in self.post_kill(key)]
        rule('resumedTone', all(tones.values()) and all(_audible(v) for t in tones.values() for v in t), tones)
        # Placed system tracks only: a SPEC-legal declaration with no start has no extent (O0.offsets).
        system = [list(extent(t)) for t in self.tracks if t.get('kind') == 'system' and _placeable(t)]
        rule('system', lost is not None and restored is not None and len(system) == 1 and all(
            at_event(s, lost['atMs']) and abs(e - restored['atMs']) <= C['EDGE_MS']
            for s, e in system), dict(systemMs=system))
        refusals = self.doc.get('refusals') if isinstance(self.doc.get('refusals'), list) else []
        refused = [r for r in refusals if isinstance(r, dict) and
                   (r.get('code') == 'startRejected' or r.get('detail') == 'resumeRefused')]
        rule('refusals', not refused, refused)
        # D52.5 ask once per loss and control connection (a reconnect's new hello asks once more), from
        # the driver's own copy of the control pipe (scenario.json resumeObservation). One loss here.
        every, mine = self.resume_requests()
        observed = [[r['atMs'], r['connection'], r['requestId'], r['captureId'], r['timelineOriginMs']] for r in every]
        connections = [r['connection'] for r in every]
        rule('resumeOnce', bool(every) and len(mine) == len(every) and len(set(connections)) == len(connections),
             dict(requests=observed, recording=[self.e['captureId'], self.e['timelineOriginMs']]))
        seam = self.resume_seam()
        rule('resumeAt', seam is not None and bool(mine) and all(seam[0] <= r['atMs'] <= seam[1] for r in mine),
             dict(atMs=[r['atMs'] for r in mine], lostAndRestoredMs=seam))

    # O4 remote with no packets ---------------------------------------------------------------
    def o4(self, rule):
        unkeyed = [t.get('id') for t in self.browser if t.get('sourceTrackId') is None]
        rule('keys', not unkeyed and len(self.keys) == 2, dict(keys=sorted(self.keys), unkeyed=unkeyed))
        rows = self.sources()
        want = {key: sorted(t['id'] for t in spans) for key, spans in self.keys.items()}
        have = {s.get('sourceTrackId'): sorted(s.get('trackIds') or []) for s in rows}
        outcomes = sorted(s.get('captureOutcome') for s in rows)
        rule('outcomeRows', len(rows) == 2 and have == want and outcomes == ['captured', 'captured'],
             dict(rows=have, outcomes=outcomes))
        slot = self.e['slotC']
        rule('evidence', self.e['maxSourceCount'] == 3 and slot['connectionState'] == 'connected' and
             slot['packetsReceived'] == 0, dict(maxSourceCount=self.e['maxSourceCount'], slotC=slot))

    # O5 microphone at 90 s ----------------------------------------------------------------------
    def o5(self, rule):
        chosen = self.selections()
        last = chosen[-1] if chosen else {}
        off = chosen[-2] if len(chosen) > 1 else {}
        at = last.get('atMs')
        ok = (last.get('me'), last.get('others'), last.get('reason')) == (True, True, 'user') and \
            off.get('me') is False and _number(at) and at >= C['MIC_ON_MS'] and self.bracket('meOn', at) and \
            (off.get('reason') == 'start' or _number(off.get('atMs')) and self.bracket('meOff', off['atMs']))
        rule('selection', ok, dict(selections=[[e.get('atMs'), e.get('me'), e.get('others'), e.get('reason')]
                                               for e in chosen], meOff=self.actions['meOff'], meOn=self.actions['meOn']))
        if not (_number(at) and _number(off.get('atMs'))):
            rule('micOff', False, 'needs a me:false selection followed by a me:true one')
            rule('micOn', False, 'needs a me:true selection')
            rule('micAudio', False, 'needs a me:true selection')
            return
        mic = [t for t in self.tracks if t.get('kind') in ME and _placeable(t)]  # unplaced: no extent
        quiet, on = off['atMs'] + C['ACTION_SLACK_MS'], events.placed_ms(at)
        intruding = [t['id'] for t in mic if extent(t)[0] < on - SAMPLE_MS - 1e-9 and extent(t)[1] > quiet]
        rule('micOff', not intruding, dict(windowMs=[quiet, on], intruding=intruding))
        # SPEC S-6/D52.2: the span starts at the selection's atMs; a late first buffer is a prefix gap.
        started = [t for t in mic if at_event(extent(t)[0], at) and extent(t)[1] >= on + C['PROOF_MS']]
        rule('micOn', len(started) == 1, dict(starts=[extent(t)[0] for t in mic], atMs=at, placedMs=on))
        if len(started) != 1:
            rule('micAudio', False, 'no microphone span starts at the me:true selection')
            return
        track = started[0]
        data, layout = self.f.window([track], extent(track)[0], extent(track)[1])
        size = 32 * track['sampleRate'] // 1000 * track['channels']
        loud = sum(1 for i in range(0, len(data), size) if any(data[i:i + size]))
        hz = self.e['nativeTones']['microphone']
        level = self.f.tone([track], at, at + C['PROOF_MS'], hz)
        rule('micAudio', loud >= 32 and _audible(level), dict(nonZero32msFrames=loud, toneHz=hz, tone=level))

    # O6 clock sanity ----------------------------------------------------------------------------
    def o6(self, rule):
        missing = [t.get('id') for t in self.browser if t.get('clockMethod') not in ('outputTimestamp', 'roundTrip')]
        rule('clockMethod', not missing, missing or 'every remote span')
        end = self.last_frame()
        rule('lastFrame', end is not None and self.bracket('stop', end, C['ACTION_SLACK_MS']),
             dict(endMs=end, stop=self.actions['stop']))

    # O7 corpus shape guard ------------------------------------------------------------------------
    def o7(self, rule):
        start = extent(self.keys[self.key('B')][0])[0]
        rule('late', start >= C['LATE_REMOTE_MS'], start)
        lengths = [extent(t)[1] - extent(t)[0] for t in self.keys[self.key('A')]]
        rule('spanRatio', len(lengths) >= 2 and max(lengths) >= C['SPAN_RATIO_MIN'] * min(lengths), lengths)
        first = self.keys[self.key('A')][0]
        data, (rate, channels) = self.f.window([first], *extent(first))
        size = rate // 10 * channels
        floor = (32768 * 10 ** (C['SILENT_DBFS'] / 20)) ** 2
        blocks = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
        audible = sum(1 for block in blocks if sum(map(mul, block, block)) / size > floor)
        density = audible / len(blocks) if blocks else 1.0
        rule('density', density < C['SPARSE_DENSITY_MAX'], round(density, 4))

    def facts(self):
        """What `compare` lines up across apps."""
        def safe(function):
            try:
                return function()
            except (AssertionError, KeyError, TypeError, ValueError, IndexError):
                return None
        lost, restored = safe(self.lost), safe(self.restored)
        chosen = self.selections()
        mic_on = chosen[-1]['atMs'] if chosen and chosen[-1].get('me') is True else None
        b_start = safe(lambda: extent(self.keys[self.key('B')][0])[0])
        end = self.events[-1] if self.events and self.events[-1].get('type') == 'end' else {}
        outcomes = {s.get('sourceTrackId'): s.get('captureOutcome') for s in self.sources()}
        role = {key: r for r, keys in self.roles.items() for key in keys}
        return dict(
            written=dict(othersSource=[[e.get('source'), e.get('reason')] for e in self.others_events()]),
            categorical=dict(
                othersSource=self.others_form(),
                selection=[[e.get('me'), e.get('others'), e.get('reason')] for e in chosen],
                state=self.doc.get('state'), problemKind=self.doc.get('problemKind'), endReason=end.get('reason'),
                remoteSources=len(self.keys), resumeRequests=len(self.resume_requests()[1]),
                spans=dict(A=[len(self.keys[k]) for k in self.roles['A']], B=[len(self.keys[k]) for k in self.roles['B']],
                           microphone=sum(t.get('kind') in ME and _placeable(t) for t in self.tracks),
                           system=sum(t.get('kind') == 'system' and _placeable(t) for t in self.tracks)),
                outcomes=sorted([role.get(k, 'other'), v] for k, v in outcomes.items()),
            ),
            numeric=dict(
                bStartMs=b_start, lostMs=lost and lost['atMs'], restoredMs=restored and restored['atMs'],
                micOnMs=mic_on, resumeLatencyMs=restored['atMs'] - lost['atMs'] if lost and restored else None,
            ),
        )


CHECKS = ('o0', 'o1', 'o2', 'o3', 'o4', 'o5', 'o6', 'o7')


def check(folder, evidence):
    """The result document for one folder; raises Unusable when it cannot be checked."""
    run = Run(Folder(folder), evidence)
    results = []
    for name in CHECKS:
        rules = []

        def rule(rule_id, passed, measured):
            rules.append(dict(rule=f'{name.upper()}.{rule_id}', passed=bool(passed), measured=measured))
        try:
            getattr(run, name)(rule)
        except (AssertionError, KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as error:
            rules.append(dict(rule=f'{name.upper()}.error', passed=False, measured=f'{type(error).__name__}: {error}'))
        failed = [r['rule'] for r in rules if not r['passed']]
        results.append({'id': name.upper(), 'pass': not failed, 'failed': failed,
                        'measured': {r['rule']: dict(passed=r['passed'], value=r['measured']) for r in rules}})
    return {'oracle': 'live-folder/1', 'app': evidence['app'], 'folder': run.f.root.resolve().name,
            'protocolCommit': evidence['protocolCommit'], 'extensionVersion': evidence['extensionVersion'],
            'constants': {k: C[k] for k in sorted(C)}, 'pass': all(r['pass'] for r in results),
            'checks': results, 'facts': run.facts()}


def compare(first, second):
    """Line two results up. Returns (exit code, report)."""
    for name in ('protocolCommit', 'extensionVersion'):
        if first.get(name) != second.get(name):
            return 2, dict(refused=f'{name} differs', a=first.get(name), b=second.get(name))
    report = dict(apps=[first.get('app'), second.get('app')], categorical={}, numeric={})
    ok = True
    one, two = first['facts'], second['facts']
    for name in sorted(set(one['categorical']) | set(two['categorical'])):
        a, b = one['categorical'].get(name), two['categorical'].get(name)
        report['categorical'][name] = dict(a=a, b=b, same=a == b)
        ok &= a == b
    for name in sorted(set(one['numeric']) | set(two['numeric'])):
        a, b = one['numeric'].get(name), two['numeric'].get(name)
        within = _number(a) and _number(b) and abs(a - b) <= COMPARE_BAND_MS
        report['numeric'][name] = dict(a=a, b=b, deltaMs=b - a if _number(a) and _number(b) else None,
                                       withinBandMs=COMPARE_BAND_MS, within=within)
        ok &= within
    passes = [first.get('pass'), second.get('pass')]
    report['checksPass'] = passes
    ok &= all(p is True for p in passes)
    report['pass'] = ok
    return (0 if ok else 1), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    one = commands.add_parser('check')
    one.add_argument('folder', type=Path)
    one.add_argument('--evidence', type=Path, required=True)
    one.add_argument('--out', type=Path, required=True)
    two = commands.add_parser('compare')
    two.add_argument('a', type=Path)
    two.add_argument('b', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'compare':
            code, report = compare(*(_strict(p.read_text(encoding='utf-8'), str(p)) for p in (args.a, args.b)))
            print(json.dumps(report, indent=1, sort_keys=True))
            return code
        result = check(args.folder, load_evidence(args.evidence))
    except (Unusable, OSError) as error:
        print(f'unusable input: {error}', file=sys.stderr)
        return 2
    for item in result['checks']:
        print(json.dumps({'id': item['id'], 'pass': item['pass'], 'measured': item['measured']}, sort_keys=True))
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    print('PASS' if result['pass'] else 'FAIL ' + ' '.join(r for c in result['checks'] for r in c['failed']))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
