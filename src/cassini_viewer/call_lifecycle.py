# SPDX-License-Identifier: GPL-3.0-or-later
"""D33 per-capture monotonic deadlines; browser facts never own device changes."""

from dataclasses import dataclass

CALL_END_GRACE = 2
BROWSER_RETURN_GRACE = 120


@dataclass
class CallLifecycle:
    missing: float | None = None
    ended: float | None = None
    closed: float | None = None

    def decide(self, now, page, count, *, fallback, others, restore_ready):
        if page == "closed" and self.closed is None:
            self.closed = now
        if self.closed is not None:
            return "stop:tabClosed" if now - self.closed >= CALL_END_GRACE else None
        # Test the stored deadline before a returning page can clear it.
        if self.missing is not None and now - self.missing >= BROWSER_RETURN_GRACE:
            return "stop:pageGone"
        if page == "present" and count > 0:
            self.missing = self.ended = None
            return "restore" if fallback and others and restore_ready else None
        if page != "present" or self.missing is not None or fallback:
            self.ended = None
            if self.missing is None:
                self.missing = now
            return "fallback" if now - self.missing >= CALL_END_GRACE and not fallback and others else None
        if self.ended is None:
            self.ended = now
        return "stop:callEnded" if now - self.ended >= CALL_END_GRACE else None
