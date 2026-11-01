# SPDX-License-Identifier: GPL-3.0-or-later
"""Append-only browser source accounting; silence is received audio, padding is not."""

from fractions import Fraction

from .recording_session import KINDS, ProtocolError, label_text, optional_string, source_track_identity


class CaptureRecord:
    def __init__(self):
        self.seen = {}
        self.received = {}
        self.bindings = {}
        self.io_failed = False
        # The app records Me natively: an announced page microphone (local)
        # source is never expected, so it cannot mark the capture interrupted.
        self.native_me = False

    def _expectable(self, kind):
        return not (self.native_me and kind == "local")

    def observe(self, identity, kind, label, *, failed=False, expected=True):
        identity = source_track_identity(identity)
        if identity is None or not isinstance(kind, str) or kind not in KINDS or type(failed) is not bool:
            raise ProtocolError("source", "Invalid discovered audio source")
        label = label_text(optional_string(label, kind, "source"))
        source = self.seen.setdefault(identity, dict(kind=kind, label=label, failed=False, expected=False))
        if source["kind"] != kind:
            raise ProtocolError("source", "Conflicting source kind")
        source["failed"] |= failed
        source["expected"] = source.get("expected", True) or expected and self._expectable(kind)

    def discover(self, sources, *, expected=lambda _kind: True):
        if not isinstance(sources, list):
            raise ProtocolError("source", "Invalid discovered audio sources")
        # Validate the entire request before changing the durable accounting.
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
                failed=source.get("failed", False),
                expected=expected(source["kind"]),
            )
        self.seen = proposed.seen

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
                    **({"captureExpected": False} if not source.get("expected", True) else {}),
                )
            )
        manifest["captureOutcome"] = dict(sources=sources)
        return any(source.get("captureExpected", True) and source["captureOutcome"] != "captured" for source in sources)
