# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit synthetic verified evidence for positive automatic-policy fixtures."""

from cassini_talk.call_admission import CAPABILITIES


def alias(row):
    return dict(
        row,
        documentGeneration=row.get("documentGeneration", "hook-fixture"),
        browserDocumentId=row.get("browserDocumentId", "chrome-fixture"),
        callGeneration=row.get("callGeneration", "call-fixture"),
        pageState="present",
    )


def signal(row):
    return dict(
        tabId=row["tabId"],
        url=row["url"],
        documentGeneration=row["browserDocumentId"],
        sourceDocumentGeneration=row["documentGeneration"],
        remoteAudio="separated",
        callEvidence=dict(
            state="active",
            reason="activeCall",
            adapter=dict(name="webrtc-interactive", version="fixture-1"),
            callGeneration=row["callGeneration"],
        ),
    )


def verify(service, peer, meetings, *, sequence=None):
    """A real accepted snapshot; callers must explicitly opt this fixture in."""
    if not peer.verified_configured:
        service.update(peer, dict(type="hello", version=1, capabilities=sorted(peer.capabilities | CAPABILITIES)))
        service.update(peer, dict(type="configured", capabilities=sorted(CAPABILITIES)))
    sequence = peer.signals_sequence + 1 if sequence is None else sequence
    service.update(
        peer,
        dict(
            type="callSignals",
            browserInstance="verified-fixture-profile",
            sequence=sequence,
            tabs=[signal(alias(row)) for row in meetings],
        ),
    )
