# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent meeting state and its single derived badge (HARMONY §3)."""

from dataclasses import dataclass

from . import ux_formats


@dataclass(frozen=True)
class MeetingError:
    id: str
    action: str | None


@dataclass(frozen=True)
class Badge:
    kind: str
    text: str
    tint: str
    dot: bool = False

    def as_dict(self):
        value = dict(kind=self.kind, text=self.text, tint=self.tint)
        if self.dot:
            value["dot"] = True
        return value


@dataclass(frozen=True)
class MeetingState:
    lifecycle: str = "saved"
    origin: str = "own"
    interrupted_at_ms: int | None = None
    transcript: str = "none"
    eta_minutes: int | None = None
    error: MeetingError | None = None

    def __post_init__(self):
        if self.lifecycle not in ("recording", "saving", "saved"):
            raise ValueError("Invalid meeting lifecycle")
        if self.origin not in ("own", "received"):
            raise ValueError("Invalid meeting origin")
        if self.transcript not in ("none", "queued", "running", "done", "failed", "damaged"):
            raise ValueError("Invalid transcript state")
        if self.interrupted_at_ms is not None and (
            type(self.interrupted_at_ms) is not int or self.interrupted_at_ms < 0
        ):
            raise ValueError("Invalid interrupted position")
        if self.eta_minutes is not None and (type(self.eta_minutes) is not int or self.eta_minutes < 0):
            raise ValueError("Invalid transcript ETA")
        if self.transcript not in ("queued", "running") and self.eta_minutes is not None:
            raise ValueError("Only a queued or running transcript has an ETA")

    @classmethod
    def from_fields(cls, fields):
        error = fields.get("error")
        return cls(
            lifecycle=fields["lifecycle"],
            origin=fields["origin"],
            interrupted_at_ms=fields.get("interruptedAtMs"),
            transcript=fields["transcript"],
            eta_minutes=fields.get("etaMinutes"),
            error=MeetingError(**error) if error is not None else None,
        )

    @property
    def can_write_down(self):
        return self.lifecycle == "saved" and self.transcript in ("none", "failed")

    def badge(self, *, live_ms=0):
        if self.lifecycle == "recording":
            return Badge("recording", f"Recording {ux_formats.live_badge(live_ms)}", "red", dot=True)
        if self.lifecycle == "saving":
            return Badge("saving", "Saving…", "grey")
        if self.transcript in ("queued", "running"):
            return Badge("transcribing", ux_formats.eta(self.eta_minutes), "accent")
        if self.error is not None and self.error.action is not None:
            return Badge("needsAttention", "Needs attention", "amber")
        if self.interrupted_at_ms is not None:
            return Badge("interrupted", f"Interrupted at {ux_formats.position(self.interrupted_at_ms)}", "amber")
        if self.origin == "received":
            return Badge("received", "Received", "grey")
        return None
