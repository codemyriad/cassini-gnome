# SPDX-License-Identifier: GPL-3.0-or-later
"""D33 per-capture monotonic deadlines; browser facts never own device changes."""

from dataclasses import dataclass, field

CALL_END_GRACE = 2
BROWSER_RETURN_GRACE = 120
# D43: a control-worker reconnect is separate from losing page/audio input.
CONTROL_RECONNECT_GRACE = 10
OTHERS_GONE_GRACE = 30


@dataclass(frozen=True)
class ResumeInputs:
    """D52.5 facts the service computes for one tick; this module does no I/O.

    ready: the peer negotiated resume-capture-v1, the recording is on
    system/lost with its audio transport closed, the row is of the recording's
    own callGeneration, the extension released the tab, and no lifecycle task
    is pending for it. loss and connection key the ask: once per loss and
    control connection.
    """

    ready: bool
    loss: int
    connection: str


@dataclass
class CallLifecycle:
    missing: float | None = None
    ended: float | None = None
    closed: float | None = None
    heard: bool = False
    # The loss being asked about, and every control connection already asked
    # for it. Never a timer: a refused or pending ask is repeated only for a
    # new loss or a connection not yet asked, even after returning to an
    # earlier connection that is still open.
    asked_loss: int | None = None
    asked_on: set = field(default_factory=set)

    def decide(
        self,
        now,
        page,
        count,
        *,
        fallback,
        others,
        restore_ready,
        fallback_pending=False,
        multistream=False,
        heard=False,
        inventory_known=True,
        control_alive=True,
        pcm_live=False,
        initial_missing=False,
        resume=None,
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
                fallback_pending=fallback_pending,
                inventory_known=inventory_known,
                control_alive=control_alive,
                initial_missing=initial_missing,
                resume=resume,
            )
        if page == "present" and count > 0:
            self.missing = self.ended = None
            return "restore" if (fallback or fallback_pending) and others and restore_ready else None
        if page != "present" or self.missing is not None:
            self.ended = None
            if self.missing is None:
                self.missing = now
            return "fallback" if now - self.missing >= CALL_END_GRACE and not fallback and others else None
        if self.ended is None:
            self.ended = now
        return "stop:callEnded" if now - self.ended >= CALL_END_GRACE else None

    def _multistream(
        self,
        now,
        page,
        count,
        *,
        fallback,
        others,
        restore_ready,
        inventory_known,
        control_alive,
        initial_missing,
        resume=None,
        fallback_pending=False,
    ):
        if page == "present" and control_alive and inventory_known and count > 0:
            self.missing = self.ended = None
            if fallback_pending and others and restore_ready:
                # D52: the call returned before its fallback committed. The
                # return cancels it, heard or not: nothing else would record.
                return "restore"
            if self.heard and fallback and others and restore_ready:
                return "switch:browser:callConnected" if initial_missing else "restore"
            if resume is not None and resume.ready and fallback and others and self.heard:
                if self.asked_loss != resume.loss:
                    self.asked_loss, self.asked_on = resume.loss, set()
                if resume.connection not in self.asked_on:
                    # D52.5: ask once per loss and control connection.
                    self.asked_on.add(resume.connection)
                    return "askResume"
            return None
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
