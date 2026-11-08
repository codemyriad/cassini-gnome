# SPDX-License-Identifier: GPL-3.0-or-later
"""D80 verified metadata admission; all times are local monotonic seconds."""

from dataclasses import dataclass

CAPABILITY = "verified-auto-admission-v1"
CAPABILITIES = frozenset(("call-signals-v1", CAPABILITY))
LEASE = 5.0


def token(value):
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 200


@dataclass(frozen=True)
class Binding:
    profile: str
    tab: int
    browser_document: str
    source_document: str
    generation: str

    @classmethod
    def read(cls, peer, row):
        evidence = row.get("callEvidence", {})
        fields = (
            peer.signals_instance,
            row.get("documentGeneration"),
            row.get("sourceDocumentGeneration"),
            evidence.get("callGeneration"),
        )
        if not all(token(value) for value in fields) or type(row.get("tabId")) is not int:
            return None
        return cls(fields[0], row["tabId"], *fields[1:])

    def same_call(self, other):
        return other is not None and (self.profile, self.tab, self.generation) == (
            other.profile,
            other.tab,
            other.generation,
        )

    def matches(self, meeting):
        return (
            meeting.get("tabId") == self.tab
            and meeting.get("browserDocumentId") == self.browser_document
            and meeting.get("documentGeneration") == self.source_document
            and meeting.get("callGeneration") == self.generation
        )


@dataclass(frozen=True)
class Proof:
    binding: Binding
    remote: str
    received: float

    @property
    def expires(self):
        return self.received + LEASE
