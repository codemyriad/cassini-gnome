# SPDX-License-Identifier: GPL-3.0-or-later
"""Append-only browser source accounting; silence is received audio, padding is not."""

import threading
from fractions import Fraction

from .recording_session import (
    KINDS,
    ProtocolError,
    browser_label_source,
    label_text,
    optional_string,
    source_track_identity,
)


class CaptureRecord:
    def __init__(self):
        self.seen = {}
        self.received = {}
        self.bindings = {}
        self.io_failed = False
        # The app records Me natively: an announced page microphone (local)
        # source is never expected, so it cannot mark the capture interrupted.
        self.native_me = False
        # R93-2: a browser Stop's source summary is validated without the
        # capture lock, which a stalled checkpoint can hold. This leaf lock
        # orders that validation against every new source identity, and the
        # first Stop marked under it seals the identities: none but its own
        # summary's joins after it, so the summary validated is the one merged
        # under the capture lock later.
        self._identities = threading.Lock()
        self.sealed = False
        # #230: the sealing Stop's validated summary, kept at its admission
        # until it is merged: by its own Stop, or by close() when another
        # Stop (or any other path) finishes the capture first.
        self._admitted = None
        self.closed = False
        self.admitted_merged = False

    def _expectable(self, kind):
        return not (self.native_me and kind == "local")

    def observe(self, identity, kind, label, *, label_source=None, failed=False, expected=True):
        identity = source_track_identity(identity)
        if identity is None or not isinstance(kind, str) or kind not in KINDS or type(failed) is not bool:
            raise ProtocolError("source", "Invalid discovered audio source")
        browser_label_source(label_source)
        label = label_text(optional_string(label, kind, "source"))
        with self._identities:
            if self.sealed and identity not in self.seen:
                raise ProtocolError("state", "No active recording")  # It came after an admitted Stop.
            source = self.seen.setdefault(
                identity,
                dict(
                    kind=kind,
                    label=label,
                    failed=False,
                    expected=False,
                    **({"labelSource": label_source} if label_source is not None else {}),
                ),
            )
        if source["kind"] != kind:
            raise ProtocolError("source", "Conflicting source kind")
        source["failed"] |= failed
        source["expected"] = source.get("expected", True) or expected and self._expectable(kind)

    def discover(self, sources, *, expected=lambda _kind: True, admitted=False):
        """Merge discovered sources. `admitted`: the summary of the Stop that
        sealed the identities (admit_stop()), which alone may add identities
        once they are sealed. Later nonempty summaries cannot rewrite existing sources either."""
        # Validate the entire request before changing the durable accounting.
        proposed = self._proposed(sources, expected)
        with self._identities:
            if self.sealed and not admitted and sources:
                raise ProtocolError("state", "No active recording")  # It came after an admitted Stop.
            self.seen = proposed.seen

    def admit_stop(self, sources, mark, *, expected=lambda _kind: True):
        """R93-2: a browser Stop's summary, validated without the capture lock.

        Returns (refusal, marked, sealing): its refusal (a ProtocolError)
        without calling mark(), or None, what mark() returned, and whether
        this Stop sealed the identities. The first Stop that mark() marks seals
        them in the same critical section, so no source seen after its
        validation can make its merge under the capture lock refuse it once
        its moment is the boundary. Only that Stop may add identities after
        the seal (discover(admitted=True)): a later Stop was validated without
        the sealing Stop's summary, which may name its new sources with
        another kind, so its own new sources are refused instead, whichever
        merges first. mark() may raise: nothing is sealed.

        #230: the sealing Stop's summary is kept here at its admission, with
        `expected` (read when it is merged). Whichever Stop or path finishes
        the capture merges it first (close()), so a later Stop that takes the
        capture lock first cannot save without it. A closed record admits
        nothing: (None, False, False), as for a finished session."""
        with self._identities:
            if self.closed:
                return None, False, False
            try:
                self._proposed(sources, lambda _kind: True)
            except ProtocolError as error:
                return error, False, False
            marked = mark()
            sealing = marked and not self.sealed
            self.sealed = self.sealed or marked
            if sealing:
                self._admitted = [dict(source) for source in sources], expected
        return None, marked, sealing

    def merge_admitted(self):
        """Merge the sealing Stop's kept summary, once (capture lock held). It
        was validated against every identity it can meet: none but its own
        joins after the seal, and a seen source never changes kind."""
        with self._identities:
            admitted, self._admitted = self._admitted, None
        if admitted is not None:
            sources, expected = admitted
            self.discover(sources, expected=expected, admitted=True)
            self.admitted_merged = True

    def close(self):
        """#230: the capture is finishing (capture lock held): merge the sealing
        Stop's summary if its own Stop has not yet, and admit no Stop after.
        Every path that finishes the session comes here first, so the saved
        outcome includes every admitted summary. A Stop sealed before this
        is merged here; none can seal after it."""
        with self._identities:
            self.closed = True
        self.merge_admitted()

    def _proposed(self, sources, expected):
        """This record with `sources` merged into a copy (it raises their refusal)."""
        if not isinstance(sources, list):
            raise ProtocolError("source", "Invalid discovered audio sources")
        proposed = CaptureRecord()
        proposed.native_me = self.native_me
        proposed.seen = {key: dict(value) for key, value in self.seen.items()}
        for source in sources:
            if not isinstance(source, dict):
                raise ProtocolError("source", "Invalid discovered audio source")
            if source.get("kind") not in ("local", "remote", "media", "tab", "application"):
                raise ProtocolError("source", "Invalid discovered audio source kind")
            if "label" in source and (not isinstance(source["label"], str) or len(source["label"]) > 200):
                raise ProtocolError("source", "Invalid discovered audio source label")
            proposed.observe(
                source.get("sourceTrackId"),
                source.get("kind"),
                source.get("label"),
                label_source=source.get("labelSource"),
                failed=source.get("failed", False),
                expected=expected(source["kind"]),
            )
        return proposed

    def admit(self, identifier, frames):
        self.received[identifier] = self.received.get(identifier, 0) + frames

    def select(self, *, me, others, browser):
        """Selection includes discovered sources with no declaration or WAV."""
        if browser:
            for source in self.seen.values():
                selected = me if source["kind"] in ("microphone", "local") else others
                if selected and self._expectable(source["kind"]):
                    source["expected"] = True

    def bind(self, identifier, identity):
        if identity in self.seen:
            self.bindings[identifier] = identity
            if self._expectable(self.seen[identity]["kind"]):
                self.seen[identity]["expected"] = True

    def fail(self, manifest, identifier, *, io=False):
        self.io_failed |= io
        for track in manifest["tracks"]:
            if track["id"] == identifier:
                track["problemKind"] = "io" if io or track.get("problemKind") == "io" else "capture"
                identity = self.bindings.get(identifier)
                if identity in self.seen:
                    self.seen[identity]["failed"] = True

    def check_overlap(self, manifest, identifier, first_frame, frames):
        track = next(t for t in manifest["tracks"] if t["id"] == identifier)
        identity = self.bindings.get(identifier)
        if identity is None:
            return
        start = Fraction(str(track["startOffsetMs"])) + Fraction(first_frame * 1000, track["sampleRate"])
        end = start + Fraction(frames * 1000, track["sampleRate"])
        for other in manifest["tracks"]:
            if (
                other is track
                or self.bindings.get(other["id"]) != identity
                or not other["frames"]
                or other.get("startOffsetMs") is None
            ):
                continue
            previous = Fraction(str(other["startOffsetMs"]))
            previous_end = previous + Fraction(other["frames"] * 1000, other["sampleRate"])
            if max(start, previous) < min(end, previous_end):
                raise ProtocolError("source", "Overlapping source spans")

    @staticmethod
    def outcome(duration, failed):
        return "failed" if failed else "neverAudio" if not duration else "lowAudio" if duration < 1000 else "captured"

    def finish(self, manifest):
        sources = []
        for identity, source in self.seen.items():
            tracks = [t for t in manifest["tracks"] if self.bindings.get(t["id"]) == identity]
            duration = Fraction(0)
            failed = source["failed"]
            for track in tracks:
                frames = self.received.get(track["id"], 0)
                milliseconds = Fraction(frames * 1000, track["sampleRate"])
                duration += milliseconds
                track["receivedFrames"] = frames
                track["captureOutcome"] = self.outcome(milliseconds, bool(track.get("problemKind")))
                failed |= bool(track.get("problemKind"))
            sources.append(
                dict(
                    sourceTrackId=identity,
                    kind=source["kind"],
                    label=source["label"],
                    captureOutcome=self.outcome(duration, failed),
                    audioMs=float(duration),
                    trackIds=[t["id"] for t in tracks],
                    **({"labelSource": source["labelSource"]} if "labelSource" in source else {}),
                    **({"captureExpected": False} if not source.get("expected", True) else {}),
                )
            )
        manifest["captureOutcome"] = dict(sources=sources)
        return any(source.get("captureExpected", True) and source["captureOutcome"] != "captured" for source in sources)
