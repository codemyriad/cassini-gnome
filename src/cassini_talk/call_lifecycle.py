# SPDX-License-Identifier: GPL-3.0-or-later
"""D33 per-capture monotonic deadlines; browser facts never own device changes."""

from dataclasses import dataclass

CALL_END_GRACE = 2
BROWSER_RETURN_GRACE = 120
# D43: a control-worker reconnect is separate from losing page/audio input.
CONTROL_RECONNECT_GRACE = 10
OTHERS_GONE_GRACE = 30


@dataclass
class CallLifecycle:
    missing: float | None = None
    ended: float | None = None
    closed: float | None = None
    heard: bool = False

    def decide(
        self,
        now,
        page,
        count,
        *,
        fallback,
        others,
        restore_ready,
        multistream=False,
        heard=False,
        inventory_known=True,
        control_alive=True,
        pcm_live=False,
    ):
        self.heard |= heard
        if page == "closed" and self.closed is None:
            self.closed = now
        if self.closed is not None:
            return "stop:tabClosed" if now - self.closed >= CALL_END_GRACE else None
        if multistream and not control_alive and pcm_live:
            # A missing control worker is not page loss while the retained
            # browser audio transport is still admitting remote samples.
            self.missing = self.ended = None
            return None
        # Test the stored deadline before a returning page can clear it.
        if self.missing is not None and now - self.missing >= BROWSER_RETURN_GRACE:
            return "stop:pageGone"
        if multistream:
            return self._multistream(
                now,
                page,
                count,
                fallback=fallback,
                others=others,
                restore_ready=restore_ready,
                inventory_known=inventory_known,
                control_alive=control_alive,
            )
        if page == "present" and count > 0:
            self.missing = self.ended = None
            return "restore" if fallback and others and restore_ready else None
        if page != "present" or self.missing is not None:
            self.ended = None
            if self.missing is None:
                self.missing = now
            return "fallback" if now - self.missing >= CALL_END_GRACE and not fallback and others else None
        if self.ended is None:
            self.ended = now
        return "stop:callEnded" if now - self.ended >= CALL_END_GRACE else None

    def _multistream(self, now, page, count, *, fallback, others, restore_ready, inventory_known, control_alive):
        if page == "present" and control_alive and inventory_known and count > 0:
            self.missing = self.ended = None
            return "restore" if self.heard and fallback and others and restore_ready else None
        if page != "present" or not control_alive or self.missing is not None:
            self.ended = None
            if self.missing is None:
                self.missing = now
            return "fallback" if now - self.missing >= CALL_END_GRACE and not fallback and others else None
        if not inventory_known:
            # Neither a missing row nor a failed lookup is a zero-source vote.
            self.ended = None
            return None
        if self.heard:
            if self.ended is None:
                self.ended = now
            if now - self.ended >= OTHERS_GONE_GRACE:
                return "stop:callEnded"
        return None
