#!/usr/bin/env python3
"""Materialize the live-folder oracle's synthetic run (shared test 2) and its mutants.

    python3 conformance/live-folder/make.py OUT                 # OUT/Recording-<id>/, OUT/evidence.json
    python3 conformance/live-folder/make.py --mutant NAME OUT   # one named defect (MUTANTS below)
    python3 conformance/live-folder/make.py --variant NAME OUT  # one other correct form (VARIANTS below)
    python3 conformance/live-folder/make.py --all OUT           # OUT/base/, OUT/<mutant>/ and OUT/<variant>/

The base folder is what a correct writer produces for the scenario: 8 kHz,
about 110 s, A muted for 60 s, B late, Me off then on, a forced resume with a
system fallback in between, then Stop. Each mutant changes that run in one way
and must fail exactly its listed rules in check.py; each variant is another form a
correct writer may produce and must pass (tests/test_live_folder_oracle.py).

Tones are integer sine tables computed with Decimal (no platform libm), stereo
tracks carry the same samples on both channels, and JSON floats are exact
binary values: Linux and macOS write identical bytes (materialized.sha256).
"""
import argparse
from array import array
import copy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import json
from math import gcd
from pathlib import Path
import sys
import wave

HERE = Path(__file__).resolve().parent
SCENARIO = json.loads((HERE / 'scenario.json').read_text())
RATE = 8000
SESSION = '02000000-0000-4000-8000-000000000002'
A_KEY, B_KEY, C_KEY, NEW_KEY = 'doc-1:source:1', 'doc-1:source:2', 'doc-1:source:3', 'doc-1:source:4'
TONE_A = ((SCENARIO['constants']['TONE_A_HZ'], 8000),)
TONE_B = ((SCENARIO['constants']['TONE_B_HZ'], 8000),)
MIC_HZ = 660
MIC = ((MIC_HZ, 6000),)
SYSTEM = ((TONE_A[0][0], 4000), (TONE_B[0][0], 4000))  # the sink monitor carries the tab's mix
PI = Decimal('3.14159265358979323846264338327950288419716939937510')
ACTIONS = {
    'record': [-180, 1350], 'meOff': [2000, 2700], 'mute': [5310, 5320], 'unmute': [66050, 68320],
    'lateRemote': [68400, 70950], 'meOn': [90200, 92650], 'kill': [95300, 96680],
    'restore': [96680, 102380], 'stop': [109500, 110310],
}
END_MS = 109625
LOST_MS = 95912.5  # the committed loss: system/lost, the seam a resume names (lostAtMs, R94)
CAPTURE_ID, ORIGIN = 'live-folder-synthetic', 1793880000000


def _sin(x):
    """sin(x) for |x| <= pi, Taylor series in 40-digit Decimal."""
    with localcontext() as context:
        context.prec = 40
        term = total = x
        n = 1
        while abs(term) > Decimal('1e-35'):
            term = -term * x * x / ((2 * n) * (2 * n + 1))
            total += term
            n += 1
        return total


_TABLES = {}


def table(hz, amplitude):
    """One period of round(amplitude * sin(2 pi hz i / RATE)), half-even, as PCM16."""
    if (hz, amplitude) not in _TABLES:
        period = RATE // gcd(RATE, hz)
        values = array('h')
        for i in range(period):
            step = i * hz % RATE
            angle = 2 * PI * step / RATE
            if angle > PI:
                angle -= 2 * PI
            values.append(int((amplitude * _sin(angle)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)))
        _TABLES[hz, amplitude] = values
    return _TABLES[hz, amplitude]


def frames(start, end):
    """Exact frame count between two session ms values on the RATE grid."""
    count = (Fraction(end) - Fraction(start)) * RATE / 1000
    assert count.denominator == 1 and count >= 0, (start, end)
    return int(count)


def remote(identifier, key, label, start, count, content):
    return dict(id=identifier, label=label, kind='remote', channels=2, startOffsetMs=start, frames=count,
                content=content, sourceTrackId=key, extra=dict(labelSource='browser', clockMethod='outputTimestamp',
                                                               clockUncertaintyMs=2))


def native(identifier, label, kind, channels, start, count, content, gaps=()):
    return dict(id=identifier, label=label, kind=kind, channels=channels, startOffsetMs=start, frames=count,
                content=content, gaps=list(gaps), extra=dict(labelSource='device'))


def base():
    """The passing run: (session fields, track plans, evidence)."""
    a1, b1 = 251.0625, 68637.25
    a2, b2 = 99300.5, 99310.125
    tracks = [
        remote('span-a1', A_KEY, 'remote: participant-a', a1, frames(a1, 95350.0625), [
            (0, frames(a1, 5450.0625), TONE_A),  # the tone outlives the request by the jitter buffer
            (frames(a1, 66250.0625), frames(a1, 95350.0625), TONE_A)]),
        native('microphone-1', 'Microphone', 'microphone', 1, 0, frames(0, 2100), [(0, frames(0, 2100), MIC)]),
        remote('span-b1', B_KEY, 'remote: participant-b', b1, frames(b1, 95350), [(0, frames(b1, 95350), TONE_B)]),
        native('microphone-2', 'Microphone', 'microphone', 1, 90250, frames(90250, END_MS),
               [(400, frames(90250, END_MS), MIC)],
               [dict(atMs=90250, durationMs=50, frames=400, kind='prefix')]),
        native('system-1', 'Computer sound', 'system', 2, 95912.5, frames(95912.5, 99500),
               [(0, frames(95912.5, 99500), SYSTEM)]),
        remote('span-a2', A_KEY, 'remote: participant-a', a2, frames(a2, END_MS), [(0, frames(a2, END_MS), TONE_A)]),
        remote('span-b2', B_KEY, 'remote: participant-b', b2, frames(b2, END_MS), [(0, frames(b2, END_MS), TONE_B)]),
    ]
    session = dict(version=1, id=SESSION, createdAt='2026-11-04T12:00:00Z', title='Live folder oracle',
                   source='chromium', state='completed', events=[
                       dict(atMs=0, type='othersSource', source='browser', reason='start'),
                       dict(atMs=0, type='selection', me=True, others=True, reason='start'),
                       dict(atMs=2100, type='selection', me=False, others=True, reason='user'),
                       dict(atMs=90250, type='selection', me=True, others=True, reason='user'),
                       dict(atMs=LOST_MS, type='othersSource', source='system', reason='lost'),
                       dict(atMs=99500, type='othersSource', source='browser', reason='restored'),
                       dict(atMs=END_MS, type='end', reason='user'),
                   ])
    evidence = dict(scenario='live-folder/1', app='synthetic', protocolCommit='0' * 40, extensionVersion='0.2.12',
                    captureId=CAPTURE_ID, timelineOriginMs=ORIGIN,
                    newFolders=[f'Recording-{SESSION}'],
                    # The app's one ask, read off the control pipe before browser/restored, naming
                    # the committed system/lost as the app wrote it.
                    resumeRequests=[resume_request(97210.5, 'resume-1')],
                    actions={k: dict(requestedAtMs=a, observedAtMs=b) for k, (a, b) in ACTIONS.items()},
                    slotC=dict(connectionState='connected', packetsReceived=0), maxSourceCount=3,
                    nativeTones=dict(microphone=MIC_HZ))
    return session, tracks, evidence


def resume_request(at, request_id, capture_id=CAPTURE_ID, connection=1, lost_at=LOST_MS):
    return dict(atMs=at, via='control-pipe', connection=connection, requestId=request_id, captureId=capture_id,
                timelineOriginMs=ORIGIN, lostAtMs=lost_at)


def _track(plans, identifier):
    return next(t for t in plans if t['id'] == identifier)


def _resort(session):
    session['events'].sort(key=lambda e: (e['atMs'], e['type'] == 'end'))


def drop_restored(session, tracks, evidence):
    session['events'] = [e for e in session['events'] if e.get('reason') != 'restored']


def mute_gap(session, tracks, evidence):
    a1 = _track(tracks, 'span-a1')
    a1['gaps'] = [dict(atMs=a1['startOffsetMs'] + 237992 / 8, durationMs=2000, frames=16000, kind='jump')]
    a1['extra']['gapFrames'] = 16000


def split_mute(session, tracks, evidence):
    a1 = _track(tracks, 'span-a1')
    cut = frames(a1['startOffsetMs'], 35000.0625)
    second = copy.deepcopy(a1)
    second.update(id='span-a1b', startOffsetMs=35000.0625, frames=a1['frames'] - cut,
                  content=[(max(0, a - cut), b - cut, mix) for a, b, mix in a1['content'] if b > cut])
    a1.update(frames=cut, content=[(a, min(b, cut), mix) for a, b, mix in a1['content'] if a < cut])
    tracks.insert(tracks.index(a1) + 1, second)


def _move_mute(tracks, evidence, mute, unmute):
    """The mute requested at mute[0] and the unmute at unmute[0] (brackets [requested, observed]):
    A's zeros follow both requests by the base run's 140.0625 and 200.0625 ms."""
    a1 = _track(tracks, 'span-a1')
    evidence['actions'].update(mute=dict(requestedAtMs=mute[0], observedAtMs=mute[1]),
                               unmute=dict(requestedAtMs=unmute[0], observedAtMs=unmute[1]))
    silent, back = frames(a1['startOffsetMs'], mute[0] + 140.0625), frames(a1['startOffsetMs'], unmute[0] + 200.0625)
    a1['content'] = [(0, silent, TONE_A), (back, a1['frames'], TONE_A)]


def short_mute(session, tracks, evidence):
    # Codex #49: a 50.74 s mute, A's tone back as early, everything else consistent with it.
    _move_mute(tracks, evidence, (5310, 5320), (56050, 58320))


def early_mute(session, tracks, evidence):
    # The mute asked for at 2.81 s, before F(A) could reach MUTE_START_MS; held a full MUTE_MS.
    _move_mute(tracks, evidence, (2810, 2820), (63550, 65820))


def lost_in_mute(session, tracks, evidence):
    session['events'].append(dict(atMs=35000, type='othersSource', source='system', reason='lost'))
    _resort(session)


def b_early(session, tracks, evidence):
    b1 = _track(tracks, 'span-b1')
    b1.update(startOffsetMs=20000.25, frames=frames(20000.25, 95350))  # still ends at the kill
    b1['content'] = [(0, b1['frames'], TONE_B)]


def silent_remote(session, tracks, evidence):
    tracks.append(remote('span-c1', C_KEY, 'remote: participant-c', 70000.0625, 80000, []))


def never_audio_row(session, tracks, evidence):
    session['extraSources'] = [dict(sourceTrackId=C_KEY, kind='remote', label='remote: participant-c',
                                    captureOutcome='neverAudio', audioMs=0, trackIds=[])]


def selection_at_60s(session, tracks, evidence):
    on = next(e for e in session['events'] if e['type'] == 'selection' and e['me'] and e['reason'] == 'user')
    on['atMs'] = 60000
    _resort(session)


def mic_from_zero(session, tracks, evidence):
    first = _track(tracks, 'microphone-1')
    first.update(frames=frames(0, END_MS), content=[(0, frames(0, END_MS), MIC)])
    tracks.remove(_track(tracks, 'microphone-2'))


def overlap_same_key(session, tracks, evidence):
    _track(tracks, 'span-a2')['startOffsetMs'] = 95000.5


def resumed_new_key(session, tracks, evidence):
    _track(tracks, 'span-a2')['sourceTrackId'] = NEW_KEY


def resumed_label(session, tracks, evidence):
    _track(tracks, 'span-a2')['label'] += ' (resumed)'


def end_before_track(session, tracks, evidence):
    session['events'][-1]['atMs'] = 100000


def no_clock_method(session, tracks, evidence):
    del _track(tracks, 'span-b1')['extra']['clockMethod']


def short_wav(session, tracks, evidence):
    _track(tracks, 'microphone-1')['wavFrames'] = frames(0, 2000)


def wrong_end_offset(session, tracks, evidence):
    _track(tracks, 'microphone-1')['endShiftMs'] = 5


def local_kind(session, tracks, evidence):
    _track(tracks, 'microphone-1')['kind'] = 'local'


def duplicate_track_id(session, tracks, evidence):
    _track(tracks, 'span-a2').update(id='span-a1', file='track-span-a2.wav')


def interrupted(session, tracks, evidence):
    session.update(state='interrupted', problem='Audio went missing', problemKind='gap')


def open_track(session, tracks, evidence):
    _track(tracks, 'span-b2')['ended'] = False


def call_ended(session, tracks, evidence):
    session['events'][-1]['reason'] = 'callEnded'


def b_resumed_new_key(session, tracks, evidence):
    _track(tracks, 'span-b2')['sourceTrackId'] = NEW_KEY


def bleed(session, tracks, evidence):
    b1 = _track(tracks, 'span-b1')
    cut = frames(b1['startOffsetMs'], b1['startOffsetMs'] + 2000)
    b1['content'] = [(0, cut, TONE_B + ((TONE_A[0][0], 2000),)), (cut, b1['frames'], TONE_B)]


def tone_in_mute(session, tracks, evidence):
    a1 = _track(tracks, 'span-a1')
    a1['content'] = [(0, a1['frames'], TONE_A)]


def no_edge_tone(session, tracks, evidence):
    a1 = _track(tracks, 'span-a1')
    a1['content'][1] = (frames(a1['startOffsetMs'], 68250.0625),) + a1['content'][1][1:]


def low_audio_a(session, tracks, evidence):
    session['outcomes'] = {A_KEY: 'lowAudio'}


def resume_refused(session, tracks, evidence):
    session['refusals'] = [dict(atMs=97000, side='browser', code='startFailed', detail='resumeRefused')]


def zero_filled_loss(session, tracks, evidence):
    # The dead transport's spans run on in zeros up to their resumed spans, with no gap range.
    for name, end in (('span-a1', 99300.0625), ('span-b1', 99310)):
        span = _track(tracks, name)
        span['frames'] = frames(span['startOffsetMs'], end)


def split_after_unmute(session, tracks, evidence):
    a1 = _track(tracks, 'span-a1')
    cut = frames(a1['startOffsetMs'], 70000.0625)
    second = copy.deepcopy(a1)
    second.update(id='span-a1b', startOffsetMs=70000.0625, frames=a1['frames'] - cut,
                  content=[(max(0, a - cut), b - cut, mix) for a, b, mix in a1['content'] if b > cut])
    a1.update(frames=cut, content=[(a, min(b, cut), mix) for a, b, mix in a1['content'] if a < cut])
    tracks.insert(tracks.index(a1) + 1, second)


def system_before_lost(session, tracks, evidence):
    system = _track(tracks, 'system-1')
    system.update(startOffsetMs=95012.5, frames=frames(95012.5, 99500))
    system['content'] = [(0, system['frames'], SYSTEM)]


def system_never_ends(session, tracks, evidence):
    system = _track(tracks, 'system-1')
    system.update(frames=frames(system['startOffsetMs'], END_MS))
    system['content'] = [(0, system['frames'], SYSTEM)]


def mic_late_no_prefix(session, tracks, evidence):
    mic = _track(tracks, 'microphone-2')
    mic.update(startOffsetMs=90650, frames=frames(90650, END_MS), gaps=[])
    mic['content'] = [(0, mic['frames'], MIC)]


def no_resume(session, tracks, evidence):
    # The folder still shows the restore: only the pipe can tell that the app never asked.
    evidence['resumeRequests'] = []


def duplicate_resume(session, tracks, evidence):
    # A second ask on the same control connection: D52.5 allows one per loss and connection.
    evidence['resumeRequests'].append(resume_request(98410.25, 'resume-2'))


def wrong_capture_resume(session, tracks, evidence):
    evidence['resumeRequests'] = [resume_request(97210.5, 'resume-1', 'live-folder-another')]


def early_resume(session, tracks, evidence):
    # Asked after the kill and before the fallback committed system/lost (D52.5: wait for the commit):
    # no seam was written yet, so the ask names none (resumeCapture without lostAtMs, R94). Its pipe
    # time alone would not show it: the writer places the loss after accepted media frames.
    evidence['resumeRequests'] = [resume_request(95600, 'resume-1', lost_at=None)]


def wrong_lost_resume(session, tracks, evidence):
    # The ask names the transport loss (95400, where Mac writes none/lost), not the committed
    # system/lost at LOST_MS: a seam the host had not committed, though asked in time on the pipe.
    evidence['resumeRequests'] = [resume_request(97210.5, 'resume-1', lost_at=95400)]


def rotate(plan, *cuts):
    """GNOME's RotatingWAV form (rotating_wav.py): the track's frames in several WAVs, cut at these
    frame counts. The first piece keeps the track's file and start; each later one starts where the
    frames before it end, on the 48 kHz grid. The track lists them in `spans`."""
    edges = [0, *cuts, plan['frames']]
    plan['pieces'] = [dict(file=wav_name(plan) if index == 0 else f"track-{plan['id']}-{index}.wav", frames=last - first,
                           startOffsetMs=plan['startOffsetMs'] if index == 0 else
                           (round(plan['startOffsetMs'] * 48) + round(first * 48000 / RATE)) / 48)
                      for index, (first, last) in enumerate(zip(edges, edges[1:]))]
    return plan['pieces']


def shifted_piece(session, tracks, evidence):
    # Codex #49: B's second WAV placed 20 s later, ending at 115350 while B's track ends at 95350.
    rotate(_track(tracks, 'span-b1'), 100000)[1]['startOffsetMs'] += 20000


def overlapping_piece(session, tracks, evidence):
    # B's second WAV placed 1 s early, over the end of its first.
    rotate(_track(tracks, 'span-b1'), 100000)[1]['startOffsetMs'] -= 1000


def rotated_wavs(session, tracks, evidence):
    # GNOME at a small WAV limit: A in three pieces (cut in the mute and after it), B and the
    # second microphone span (with its prefix gap) in two.
    a1 = _track(tracks, 'span-a1')
    rotate(a1, frames(a1['startOffsetMs'], 30000.0625), frames(a1['startOffsetMs'], 70000.0625))
    rotate(_track(tracks, 'span-b1'), 100000)
    rotate(_track(tracks, 'microphone-2'), frames(90250, 100000))


def folder_leads(session, tracks, evidence):
    # The extension stamps a frame when it reaches the output (D61): A's stamps run 20 ms ahead
    # of the session clock, so F(A) passes each threshold 20 ms early and the driver's waits end
    # on the session clock instead: the mute asked at exactly MUTE_START_MS, the unmute at
    # exactly mute + MUTE_MS. A's span, with its mute edges, sits 20 ms later on the timeline.
    _move_mute(tracks, evidence, (5000, 5010), (65000, 67270))
    _track(tracks, 'span-a1')['startOffsetMs'] += 20


def fractional_mic(session, tracks, evidence):
    # GNOME and Mac write the selection's wall-clock atMs as the span start; this one
    # places 1/50 ms below it on the 16 kHz grid (D55).
    on = next(e for e in session['events'] if e['type'] == 'selection' and e['me'] and e['reason'] == 'user')
    on['atMs'] = 90250.02
    mic = _track(tracks, 'microphone-2')
    mic.update(startOffsetMs=90250.02, frames=mic['frames'] - 1)  # its last frame still ends by end.atMs
    mic['content'] = [(400, mic['frames'], MIC)]


def pre_restore_declaration(session, tracks, evidence):
    # Mac today (OwnedBrowserCapture.swift browserTrack, before restoreBrowser): the extension declares
    # A's and B's new wire ids after the forced resume, while browserAlive is still false, and each
    # declaration is committed as an ended 0-frame track with no startOffsetMs/endOffsetMs, bound to
    # its key, between system/lost and browser/restored. SPEC allows a missing start; it must never
    # abort a rule group, and only the strict rules O0.offsets and O4.outcomeRows fail it.
    at = tracks.index(_track(tracks, 'span-a2'))
    for identifier, key, label in (('declared-a', A_KEY, 'remote: participant-a'),
                                   ('declared-b', B_KEY, 'remote: participant-b')):
        tracks.insert(at, remote(identifier, key, label, None, 0, []))
        at += 1


def uuid_case(session, tracks, evidence):
    # Mac names the folder Recording-<id.uuidString.lowercased()> and JSONEncoder writes the id in
    # uppercase (RecordingSession.swift): one UUID, two cases. SPEC says only Recording-<UUID>.
    identifier = '9F3B2A1C-7D4E-4A5B-8C6D-0E1F2A3B4C5D'
    session.update(id=identifier, folder=f'Recording-{identifier.lower()}')
    evidence['newFolders'] = [session['folder']]


def mac_loss_form(session, tracks, evidence):
    # Mac writes none/lost at the transport loss, then system/lost when the fallback commits (SPEC S-6).
    session['events'].append(dict(atMs=95400, type='othersSource', source='none', reason='lost'))
    _resort(session)


# name: (defect, every rule it must fail, exactly; the first names it). With no
# jsonschema (macOS /usr/bin/python3) O0.schema reports unavailable and passes.
MUTANTS = {
    'drop-restored': (drop_restored, ['O3.restored', 'O3.sequence', 'O3.state', 'O3.system', 'O3.resumeAt']),
    'mute-gap': (mute_gap, ['O2.gaps', 'O2.present']),
    'split-mute': (split_mute, ['O2.oneSpan', 'O3.spans']),
    'lost-in-mute': (lost_in_mute, ['O2.noEvents', 'O3.sequence']),
    'b-early': (b_early, ['O1.late', 'O1.bracket', 'O7.late']),
    'silent-remote': (silent_remote, ['O4.keys', 'O4.outcomeRows']),
    'never-audio-row': (never_audio_row, ['O4.outcomeRows']),
    'selection-at-60s': (selection_at_60s, ['O5.selection', 'O5.micOn', 'O5.micAudio']),
    'mic-from-zero': (mic_from_zero, ['O5.micOff', 'O5.micOn', 'O5.micAudio']),
    'overlap-same-key': (overlap_same_key, ['O3.noOverlap', 'O3.spans', 'O3.lossAbsent', 'O3.resumedStart',
                                            'O3.resumedTone']),
    'resumed-new-key': (resumed_new_key, ['O3.spans', 'O3.resumedStart', 'O3.resumedTone', 'O4.keys',
                                          'O4.outcomeRows', 'O7.spanRatio']),
    'resumed-label': (resumed_label, ['O3.labels']),
    'end-before-track': (end_before_track, ['O0.bounds']),
    'no-clock-method': (no_clock_method, ['O6.clockMethod']),
    'short-wav': (short_wav, ['O0.files']),
    'wrong-end-offset': (wrong_end_offset, ['O0.offsets']),
    'local-kind': (local_kind, ['O0.kinds']),
    'duplicate-track-id': (duplicate_track_id, ['O0.unique', 'O0.schema']),
    'interrupted': (interrupted, ['O0.state']),
    'open-track': (open_track, ['O0.ended']),
    'call-ended': (call_ended, ['O0.end']),
    'b-resumed-new-key': (b_resumed_new_key, ['O1.oneKey', 'O3.spans', 'O3.resumedStart', 'O3.resumedTone',
                                              'O4.keys', 'O4.outcomeRows']),
    'bleed': (bleed, ['O1.bleed']),
    'tone-in-mute': (tone_in_mute, ['O2.silent', 'O7.density']),
    'no-edge-tone': (no_edge_tone, ['O2.toneEdges']),
    'low-audio-a': (low_audio_a, ['O2.outcome', 'O4.outcomeRows']),
    'resume-refused': (resume_refused, ['O3.refusals']),
    'zero-filled-loss': (zero_filled_loss, ['O3.lossAbsent']),
    'split-after-unmute': (split_after_unmute, ['O3.spans']),
    'system-before-lost': (system_before_lost, ['O3.system']),
    'system-never-ends': (system_never_ends, ['O3.system']),
    'mic-late-no-prefix': (mic_late_no_prefix, ['O5.micOn', 'O5.micAudio']),
    'short-mute': (short_mute, ['O2.duration']),
    'early-mute': (early_mute, ['O2.muteStart']),
    'no-resume': (no_resume, ['O3.resumeOnce', 'O3.resumeAt']),
    'duplicate-resume': (duplicate_resume, ['O3.resumeOnce']),
    'wrong-capture-resume': (wrong_capture_resume, ['O3.resumeOnce', 'O3.resumeAt']),
    'early-resume': (early_resume, ['O3.resumeAt']),
    'wrong-lost-resume': (wrong_lost_resume, ['O3.resumeAt']),
    'shifted-piece': (shifted_piece, ['O0.pieces']),
    'overlapping-piece': (overlapping_piece, ['O0.pieces']),
    'pre-restore-declaration': (pre_restore_declaration, ['O0.offsets', 'O4.outcomeRows']),
}

# name: another form a correct writer may produce; each must pass every check.
VARIANTS = {
    'folder-leads': folder_leads,
    'fractional-mic': fractional_mic,
    'mac-loss-form': mac_loss_form,
    'rotated-wavs': rotated_wavs,
    'uuid-case': uuid_case,
}


def run(name=None):
    session, tracks, evidence = base()
    if name is not None:
        (VARIANTS[name] if name in VARIANTS else MUTANTS[name][0])(session, tracks, evidence)
    return session, tracks, evidence


def samples(plan):
    """A track's interleaved PCM16: silence plus its content segments, track-local phase."""
    mono = array('h', bytes(2 * plan['frames']))
    for first, last, mix in plan['content']:
        assert 0 <= first <= last <= plan['frames'], (plan['id'], first, last)
        segment = None
        for hz, amplitude in mix:
            period = table(hz, amplitude)
            count, offset = last - first, first % len(period)
            part = (period * (count // len(period) + 2))[offset:offset + count]
            segment = part if segment is None else array('h', map(int.__add__, segment, part))
        mono[first:last] = segment
    if plan['channels'] == 1:
        return mono
    stereo = array('h', bytes(4 * plan['frames']))
    stereo[0::2] = mono
    stereo[1::2] = mono
    return stereo


def wav_name(plan):
    return plan.get('file', f"track-{plan['id']}.wav")


def document(session, tracks):
    """session.json as the writer checkpoints it at Stop, with captureOutcome from the tracks."""
    out = []
    for plan in tracks:
        track = dict(id=plan['id'], label=plan['label'], kind=plan['kind'], file=wav_name(plan),
                     sampleRate=RATE, channels=plan['channels'], frames=plan['frames'])
        if plan['startOffsetMs'] is not None:  # writers omit an unknown start, and so its end
            track.update(startOffsetMs=plan['startOffsetMs'], endOffsetMs=plan['startOffsetMs'] +
                         plan['frames'] / RATE * 1000 + plan.get('endShiftMs', 0))
        track['ended'] = plan.get('ended', True)
        if 'sourceTrackId' in plan:
            track['sourceTrackId'] = plan['sourceTrackId']
            track.update(receivedFrames=plan['frames'], captureOutcome='captured')
        track.update(plan['extra'])
        track['gaps'] = plan.get('gaps', [])
        if 'pieces' in plan:
            track['spans'] = [dict(file=p['file'], sampleRate=RATE, channels=plan['channels'], frames=p['frames'],
                                   startOffsetMs=p['startOffsetMs'], ended=True) for p in plan['pieces']]
        out.append(track)
    sources = {}
    for track in out:
        if 'sourceTrackId' in track:
            row = sources.setdefault(track['sourceTrackId'], dict(
                sourceTrackId=track['sourceTrackId'], kind=track['kind'], label=track['label'],
                captureOutcome=session.get('outcomes', {}).get(track['sourceTrackId'], 'captured'),
                audioMs=0, trackIds=[]))
            row['audioMs'] += track['frames'] / RATE * 1000
            row['trackIds'].append(track['id'])
    fields = {k: v for k, v in session.items() if k not in ('events', 'extraSources', 'outcomes', 'folder')}
    return dict(fields, tracks=out, events=session['events'],
                captureOutcome=dict(sources=list(sources.values()) + session.get('extraSources', [])))


def write(out, session, tracks, evidence):
    """OUT/Recording-<id>/ and OUT/evidence.json; returns the folder."""
    out = Path(out)
    folder = out / session.get('folder', f"Recording-{session['id']}")
    folder.mkdir(parents=True)
    for plan in tracks:
        data = samples(plan)[:plan.get('wavFrames', plan['frames']) * plan['channels']]
        if sys.byteorder != 'little':
            data.byteswap()
        first = 0
        for piece in plan.get('pieces') or [dict(file=wav_name(plan), frames=len(data) // plan['channels'])]:
            last = first + piece['frames']
            with wave.open(str(folder / piece['file']), 'wb') as writer:
                writer.setnchannels(plan['channels'])
                writer.setsampwidth(2)
                writer.setframerate(RATE)
                writer.writeframes(data[first * plan['channels']:last * plan['channels']].tobytes())
            first = last
    (folder / 'session.json').write_text(json.dumps(document(session, tracks), indent=1) + '\n')
    (out / 'evidence.json').write_text(json.dumps(evidence, indent=1, sort_keys=True) + '\n')
    return folder


def make(out, name=None):
    """Materialize the base run, or one named mutant or variant of it."""
    return write(out, *run(name))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--mutant', choices=sorted(MUTANTS))
    group.add_argument('--variant', choices=sorted(VARIANTS))
    group.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        for name in [None] + sorted(MUTANTS) + sorted(VARIANTS):
            print(make(args.out / (name or 'base'), name))
    else:
        print(make(args.out, args.mutant or args.variant))


if __name__ == '__main__':
    main()
