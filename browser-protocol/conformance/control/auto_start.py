"""Independent oracle for auto-start-vectors.json (D30.1, D52.5); never production code.

Desktop apps must run the literal vectors through their own automatic-start,
manual Record and resume paths; this file only cross-checks the hand-written
expectations. A lost recording matches by (tabId, callGeneration) only, never by
the control peer it was started under (`controlPeer`).
"""
LOST = {'source': 'system', 'reason': 'lost'}


def lost_recording(case):
    """D52.5: the recording on system/lost for this call, if any."""
    meeting = case['meeting']
    return next((r for r in case.get('recordings', []) if r['tabId'] == meeting['tabId']
                 and r['callGeneration'] == meeting.get('callGeneration') and r['othersSource'] == LOST), None)


def decide(case):
    capabilities, meeting = case['capabilities'], case['meeting']
    receivers = meeting['sourceCount'] > 0
    heard = meeting.get('heard') is True
    evidence = heard or 'call-evidence-v1' not in capabilities
    lost = lost_recording(case)
    # Seeded recordings start unlatched unless heardSeen says the call was heard earlier.
    latched = heard or (lost is not None and lost.get('heardSeen') is True)
    return {
        # Never a second folder for the lost call: it may only be resumed.
        'automatic': receivers and evidence and lost is None,
        # The lost call is recording: its row shows Stop and Record all skips it.
        'manual': receivers and lost is None,
        'resume': lost is not None and receivers and latched and 'resume-capture-v1' in capabilities,
    }
