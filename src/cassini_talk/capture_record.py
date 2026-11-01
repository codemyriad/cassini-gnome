# SPDX-License-Identifier: GPL-3.0-or-later
"""Append-only browser source accounting; silence is received audio, padding is not."""

from fractions import Fraction

from .recording_session import KINDS, ProtocolError, label_text, optional_string, source_track_identity


class CaptureRecord:
    def __init__(self):
        self.seen = {}
        self.received = {}
        self.io_failed = False

    def observe(self, identity, kind, label, *, failed=False):
        identity = source_track_identity(identity)
        if identity is None or not isinstance(kind, str) or kind not in KINDS or type(failed) is not bool:
            raise ProtocolError("source", "Invalid discovered audio source")
        label = label_text(optional_string(label, kind, "source"))
        source = self.seen.setdefault(identity, dict(kind=kind, label=label, failed=False))
        if source["kind"] != kind:
            raise ProtocolError("source", "Conflicting source kind")
        source["failed"] |= failed

    def discover(self, sources):
        if not isinstance(sources, list):
            raise ProtocolError("source", "Invalid discovered audio sources")
        # Validate the entire request before changing the durable accounting.
        proposed = CaptureRecord()
        proposed.seen = {key: dict(value) for key, value in self.seen.items()}
        for source in sources:
            if not isinstance(source, dict):
                raise ProtocolError("source", "Invalid discovered audio source")
            proposed.observe(
                source.get("sourceTrackId"), source.get("kind"), source.get("label"), failed=source.get("failed", False)
            )
        self.seen = proposed.seen

    def admit(self, identifier, frames):
        self.received[identifier] = self.received.get(identifier, 0) + frames

    def fail(self, manifest, identifier, *, io=False):
        self.io_failed |= io
        for track in manifest["tracks"]:
            if track["id"] == identifier:
                track["problemKind"] = "io" if io or track.get("problemKind") == "io" else "capture"
                identity = track.get("sourceTrackId") or identifier
                if identity in self.seen:
                    self.seen[identity]["failed"] = True

    @staticmethod
    def check_overlap(manifest, identifier, first_frame, frames):
        track = next(t for t in manifest["tracks"] if t["id"] == identifier)
        identity = track.get("sourceTrackId")
        if identity is None:
            return
        start = Fraction(str(track["startOffsetMs"])) + Fraction(first_frame * 1000, track["sampleRate"])
        end = start + Fraction(frames * 1000, track["sampleRate"])
        for other in manifest["tracks"]:
            if (
                other is track
                or other.get("sourceTrackId") != identity
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
            tracks = [t for t in manifest["tracks"] if (t.get("sourceTrackId") or t["id"]) == identity]
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
                )
            )
        manifest["captureOutcome"] = dict(sources=sources)
        return any(source["captureOutcome"] != "captured" for source in sources)
