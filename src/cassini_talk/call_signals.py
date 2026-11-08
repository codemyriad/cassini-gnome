# SPDX-License-Identifier: GPL-3.0-or-later
"""D74.1 call associations. No audio, GTK, or inference from playback/levels.

The broker serializes these transitions with admission and Stop under its lock.
A control incarnation orders facts; it is never the stable recording identity.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from .call_admission import Binding, Proof, token

CAPABILITY = "call-signals-v1"
LOSS_GRACE = 120
MAX_SAFE = 2**53 - 1


def bounded(value, limit):
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= limit


def rows(packet):
    """Validate a complete replacement before changing any eligibility."""
    if ("type" in packet and packet["type"] != "callSignals") or set(packet) - {
        "type",
        "browserInstance",
        "sequence",
        "tabs",
    }:
        raise ValueError("Unknown call signal fields")
    if not bounded(packet.get("browserInstance"), 200):
        raise ValueError("Missing stable browser profile identity")
    sequence, tabs = packet.get("sequence"), packet.get("tabs")
    if type(sequence) is not int or not 0 <= sequence <= MAX_SAFE or not isinstance(tabs, list) or len(tabs) > 100:
        raise ValueError("Invalid call signal snapshot")
    result = {}
    for row in tabs:
        if not isinstance(row, dict):
            raise ValueError("Invalid call signal row")
        required = {"tabId", "url", "documentGeneration", "remoteAudio", "callEvidence"}
        if not required <= set(row) or set(row) - required - {"sourceDocumentGeneration"}:
            raise ValueError("Invalid call signal fields")
        tab, url, document = row.get("tabId"), row.get("url"), row.get("documentGeneration")
        evidence, remote = row.get("callEvidence"), row.get("remoteAudio")
        if (
            type(tab) is not int
            or not 0 <= tab <= MAX_SAFE
            or tab in result
            or not bounded(url, 8192)
            or len(url) > 2048
            or urlsplit(url).scheme not in ("http", "https")
            or not urlsplit(url).hostname
            or not bounded(document, 200)
            or remote not in ("separated", "mixed", "unknown")
            or not isinstance(evidence, dict)
        ):
            raise ValueError("Invalid call signal identity")
        if set(evidence) != {"state", "reason", "adapter", "callGeneration"}:
            raise ValueError("Invalid call evidence fields")
        state, reason = evidence.get("state"), evidence.get("reason")
        adapter, generation = evidence.get("adapter"), evidence.get("callGeneration")
        adapter_ok = (
            isinstance(adapter, dict)
            and set(adapter) == {"name", "version"}
            and all(bounded(adapter.get(field), 320) and len(adapter[field]) <= 80 for field in ("name", "version"))
        )
        if (
            state not in ("active", "inactive", "unknown")
            or generation is not None
            and not bounded(generation, 200)
            or adapter is not None
            and not adapter_ok
            or (remote != "unknown" or state != "unknown")
            and not adapter_ok
            or state == "active"
            and (reason != "activeCall" or generation is None)
            or state == "inactive"
            and (reason not in ("callEnded", "nonCall") or reason == "callEnded" and generation is None)
            or state == "unknown"
            and reason not in ("unverified", "unsupported", "evidenceConflict")
        ):
            raise ValueError("Invalid call evidence")
        if "sourceDocumentGeneration" in row:
            source = row["sourceDocumentGeneration"]
            if (
                source is not None
                and not token(source)
                or (state == "active" or reason == "callEnded")
                and source is None
                or state == "unknown"
                and generation is not None
            ):
                raise ValueError("Invalid verified source binding")
        result[tab] = dict(row, callEvidence=dict(evidence, adapter=dict(adapter) if adapter_ok else None))
    return sequence, result


@dataclass
class Association:
    site: str
    lost_at: float | None = None
    binding: Binding | None = None


class SignalCalls:
    def __init__(self):
        self.owners = {}  # stable instance -> (connected_at, control id)
        self.snapshots = {}  # stable instance -> current validated tab rows
        self.withdrawals = {}  # binding -> first monotonic proof-loss instant
        self.proofs = {}  # (stable profile, tab) -> strictly accepted active receipt
        self.calls = {}  # (instance, tab, document, call) -> associated reference
        self.suppressed = set()  # (instance, tab, call generation), including across document reload
        self.capture_id = None
        self.starting = False
        self.retiring = None
        self.stopping = False

    def claim(self, peer, now):
        instance = peer.signals_instance
        if instance is None:
            return False  # No stable profile identity: never guess across reconnects.
        stamp = (peer.connected_at, peer.id)
        previous = self.owners.get(instance)
        if previous is not None and previous > stamp:
            return False
        if previous != stamp:
            self.owners[instance] = stamp
            self.snapshots.pop(instance, None)
            self._clear_proofs(instance, now)
            self._lost(instance, now)
        return True

    def current(self, peer):
        return self.owners.get(peer.signals_instance) == (peer.connected_at, peer.id)

    def disconnect(self, peer, now):
        self.expire(now)
        if self.current(peer):
            self.snapshots.pop(peer.signals_instance, None)
            self._clear_proofs(peer.signals_instance, now)
            self._lost(peer.signals_instance, now)

    def _clear_proofs(self, instance, now=None):
        for key in list(self.proofs):
            if key[0] == instance:
                proof = self.proofs.pop(key)
                if now is not None:
                    self.withdrawals.setdefault(proof.binding, min(now, proof.expires))

    def invalidate(self, peer, tab, now):
        proof = self.proofs.pop((peer.signals_instance, tab), None) if self.current(peer) else None
        if proof is not None:
            self.withdrawals.setdefault(proof.binding, min(now, proof.expires))
            for call in self.calls.values():
                if call.binding == proof.binding and call.lost_at is None:
                    call.lost_at = self.withdrawals[proof.binding]

    def proof(self, peer, tab, now, *, remote=None, meeting=None):
        if (
            not getattr(peer, "verified_configured", False)
            or not self.current(peer)
            or getattr(peer, "closed", False)
            or getattr(peer, "shut_down", None) is not None
            and peer.shut_down.is_set()
        ):
            return None
        proof = self.proofs.get((peer.signals_instance, tab))
        if proof is None or now >= proof.expires or remote is not None and proof.remote != remote:
            return None
        if meeting is not None and not proof.binding.matches(meeting):
            return None
        return proof

    def expire(self, now):
        for key, call in list(self.calls.items()):
            if call.lost_at is not None and now - call.lost_at >= LOSS_GRACE:
                self._drop(key, "pageGone")
        for (instance, tab), proof in list(self.proofs.items()):
            if now < proof.expires:
                continue
            for key, call in self.calls.items():
                if key[:2] == (instance, tab) and call.binding == proof.binding and call.lost_at is None:
                    call.lost_at = proof.expires
            self.withdrawals.setdefault(proof.binding, proof.expires)
            self.proofs.pop((instance, tab))
        for key, call in list(self.calls.items()):
            if call.lost_at is not None and now - call.lost_at >= LOSS_GRACE:
                self._drop(key, "pageGone")

    def _lost(self, instance, now):
        for key, call in self.calls.items():
            if key[0] == instance and call.lost_at is None:
                call.lost_at = now

    def _drop(self, key, reason):
        self.calls.pop(key, None)
        if not self.calls and self.capture_id is not None:
            self.retiring = self.retiring or reason

    def update(self, peer, snapshot, now):
        if not self.current(peer):
            return
        instance = peer.signals_instance
        self.expire(now)
        self.snapshots[instance] = snapshot
        self._clear_proofs(instance, now)
        if getattr(peer, "verified_configured", False):
            for tab, row in snapshot.items():
                binding = Binding.read(peer, row)
                if binding is not None and row["callEvidence"]["state"] == "active":
                    self.proofs[(instance, tab)] = Proof(binding, row["remoteAudio"], now)
                    self.withdrawals.pop(binding, None)
        for key, call in list(self.calls.items()):
            if key[0] != instance:
                continue
            row = snapshot.get(key[1])
            evidence = row["callEvidence"] if row else {}
            same = (
                row is not None
                and (row["documentGeneration"], evidence.get("callGeneration")) == key[2:]
                and Binding.read(peer, row) == call.binding
            )
            if same and evidence.get("state") == "inactive" and evidence.get("reason") == "callEnded":
                self._drop(key, "callEnded")
            elif (
                row is not None
                and evidence.get("state") == "active"
                and not same
                and evidence.get("callGeneration") != key[3]
            ):
                self._drop(key, "callEnded")  # Fence before admitting a replacement generation.
            elif same and evidence.get("state") == "active" and self.proof(peer, key[1], now) is not None:
                call.lost_at = None  # Route fixed: remoteAudio cannot hand this owner to RTP.
            elif (
                row is not None
                and evidence.get("state") == "active"
                and (proof := self.proof(peer, key[1], now)) is not None
                and proof.binding.same_call(call.binding)
                and self.capture_id is not None
                and not self.starting
            ):
                # Fresh producer-ledger continuity can update an established
                # owner's documents. A pending Start keeps its captured binding.
                self.calls.pop(key)
                call.binding, call.lost_at = proof.binding, None
                self.calls[(instance, key[1], row["documentGeneration"], key[3])] = call
            elif call.lost_at is None:
                # A full replacement, including another generation's end,
                # withdraws this association's evidence. It cannot end this
                # call immediately or extend a grace that has already begun.
                call.lost_at = now

    def advance(self, now, *, enabled):
        self.expire(now)
        for key, call in list(self.calls.items()):
            if call.lost_at is not None and now - call.lost_at >= LOSS_GRACE:
                self._drop(key, "pageGone")
        if self.starting and not enabled:
            self.retiring = self.retiring or "user"
        if self.retiring:
            return
        if enabled:
            for instance, snapshot in self.snapshots.items():
                for tab, row in snapshot.items():
                    evidence = row["callEvidence"]
                    key = (instance, tab, row["documentGeneration"], evidence.get("callGeneration"))
                    if (
                        evidence["state"] == "active"
                        and row["remoteAudio"] == "mixed"
                        and (proof := self.proofs.get((instance, tab))) is not None
                        and now < proof.expires
                        and (instance, tab, key[3]) not in self.suppressed
                    ):
                        self.calls.setdefault(key, Association(evidence["adapter"]["name"], binding=proof.binding))

    def suppress(self):
        # Snapshot storage and policy admission use separate lock holds. Stop
        # also fences eligible facts committed before that admission, including
        # while the previous shared capture is retiring.
        for instance, snapshot in self.snapshots.items():
            for tab, row in snapshot.items():
                evidence = row["callEvidence"]
                if evidence["state"] == "active":
                    self.suppressed.add((instance, tab, evidence["callGeneration"]))
        for instance, tab, _document, generation in self.calls:
            self.suppressed.add((instance, tab, generation))
        self.calls.clear()
        if self.capture_id is not None:
            self.retiring = "user"
        # Keep current Stop generations even when history exceeds the usual
        # bound; never evict a live suppressed call to admit it again.
        current = {
            (instance, tab, row["callEvidence"].get("callGeneration"))
            for instance, snapshot in self.snapshots.items()
            for tab, row in snapshot.items()
        }
        stale = self.suppressed - current
        while len(self.suppressed) > 1024 and stale:
            self.suppressed.remove(stale.pop())

    def reserve(self, capture_id):
        assert self.capture_id is None and self.calls and not self.retiring
        self.capture_id, self.starting = capture_id, True

    def started(self):
        self.starting = False

    def released(self, *, failed=False):
        if failed:
            self.suppress()
        self.capture_id = None
        self.starting = self.stopping = False
        self.retiring = None

    def owns_call(self, peer, tab, generation):
        return any(
            key[0] == peer.signals_instance and key[1] == tab and (generation is None or key[3] == generation)
            for key in self.calls
        )
