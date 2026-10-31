# SPDX-License-Identifier: GPL-3.0-or-later
"""Growing atomic snapshots with a linear bound on cumulative word writes."""

import math
import time

from .meeting_store import atomic_json
from .transcript import normalize_quote_items


class TranscriptPublisher:
    def __init__(self, directory, *, existing_count=0, clock=time.monotonic, check_active=None):
        self.directory = directory
        self.clock = clock
        self.existing_count = existing_count
        self.words = []
        self.language = "und"
        self.published = 0
        self.last_flush = None
        self.check_active = check_active

    def add(self, words, speaker, order, language):
        self.words.extend(dict(word, speaker=speaker, _order=order) for word in words)
        self.language = language

    def reassign(self, words):
        self.words = words
        self.published = -1  # Same word count, but speaker IDs have changed.

    def flush(self, *, force=False, complete=False):
        if self.check_active is not None:
            self.check_active()
        count = len(self.words)
        if count == self.published and (not complete or self.last_flush is not None):
            return False
        # During resume, keep the longer already published partial transcript
        # until recomputation catches up. A completed result may replace it.
        if count < self.existing_count and not complete:
            return False
        now = self.clock()
        if not force:
            if self.last_flush is not None and now - self.last_flush < 1:
                return False
            # At least 12.5% growth between snapshots makes cumulative full
            # sidecar writes <=~10x final size, rather than one full copy/segment.
            if self.published and count < self.published + max(32, math.ceil(self.published / 8)):
                return False
        ordered = sorted(self.words, key=lambda word: (word["startMs"], word["_order"]))
        body = normalize_quote_items({key: value for key, value in word.items() if key != "_order"} for word in ordered)
        value = dict(format="cassini.words.v1", language=self.language, items=body)
        if self.check_active is None:
            atomic_json(self.directory / "transcript.json", value)
        else:
            # Serialization may take time for a long meeting. Recheck after
            # staging/fsync, immediately before replacing the saved snapshot.
            atomic_json(self.directory / "transcript.json", value, before_commit=self.check_active)
        self.published = count
        self.last_flush = now
        return True
