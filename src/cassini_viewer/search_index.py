# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuildable, worker-owned transcript FTS. Nothing is written into the Library."""

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from html import escape
from itertools import groupby
from pathlib import Path
from zoneinfo import ZoneInfo

START, END = "\x01", "\x02"


@dataclass(frozen=True)
class SearchTurn:
    speaker: str
    words: tuple
    text: str

    @property
    def start_ms(self):
        return self.words[0].start_ms


@dataclass(frozen=True)
class SearchHit:
    key: str
    turn: int
    marked: str
    position_ms: int
    quote_markup: str
    selected_quote_markup: str
    context_markup: str


def marked_markup(value, *, selected=False):
    value = escape(value)
    colour = "#ffffff" if selected else "#fbe7a1"
    return value.replace(START, f'<span background="{colour}" foreground="#202024" weight="semibold">').replace(
        END, "</span>"
    )


def excerpt_markup(marked, *, limit=220, selected=False):
    """Bound GTK layout work, retaining the first hit and complete markup spans."""
    plain = marked.replace(START, "").replace(END, "")
    first = marked.find(START)
    first = max(0, first)
    lo = max(0, first - 70) if len(plain) > limit else 0
    hi = min(len(plain), max(lo + limit, first + 80))
    position, active, parts = 0, False, []
    for char in marked:
        if char == START:
            active = True
        elif char == END:
            active = False
        else:
            if lo <= position < hi:
                if not parts or parts[-1][0] != active:
                    parts.append((active, []))
                parts[-1][1].append(char)
            position += 1
    output = "…" if lo else ""
    for active, chars in parts:
        value = "".join(chars)
        output += marked_markup(START + value + END if active else value, selected=selected)
    return output + ("…" if hi < len(plain) else "")


def date_range(choice, now_ms, tz):
    if choice == "any":
        return None
    days = {"today": 1, "week": 7, "month": 30}[choice]
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    today = datetime.fromtimestamp(now_ms / 1000, zone).date()
    lower = datetime.combine(today - timedelta(days=days - 1), time.min, zone)
    upper = datetime.combine(today + timedelta(days=1), time.min, zone)
    return int(lower.timestamp() * 1000), int(upper.timestamp() * 1000)


class SearchIndex:
    """One instance and SQLite connection belong to one worker thread."""

    def __init__(self, root, cache=None, *, cancelled=None):
        root = str(Path(root).absolute())
        cache = Path(cache or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassini")
        if cache.resolve() == Path(root).resolve() or Path(root).resolve() in cache.resolve().parents:
            raise ValueError("Search cache must be outside the Library")
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = cache / ("search-" + hashlib.sha256(root.encode()).hexdigest()[:16] + ".sqlite3")
        self.cancelled = cancelled
        self.db = sqlite3.connect(self.path)
        try:
            valid = self.db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        except sqlite3.DatabaseError as error:
            if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                self.db.close()
                raise
            valid = False
        if not valid:
            self.db.close()
            self.path.unlink()
            self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        if cancelled is not None:
            self.db.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, started INTEGER, origin TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS hits USING fts5(
                body, key UNINDEXED, turn UNINDEXED, speaker UNINDEXED, tokenize='unicode61'
            );
        """)
        self.current = {}
        self.turns = {}
        self.signatures = {}

    def sync(self, snapshot):
        meetings = {m.key: m for m in snapshot.meetings} if not snapshot.unavailable else {}
        stored = dict(self.db.execute("SELECT key, fingerprint FROM meetings"))
        with self.db:
            for key in stored.keys() - meetings.keys():
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.execute("DELETE FROM meetings WHERE key = ?", (key,))
                self.turns.pop(key, None)
                self.signatures.pop(key, None)
            for key, meeting in meetings.items():
                self._check_cancelled()
                # Scanner keeps immutable word tuples alive. Polling capture levels
                # never hashes or rewrites an unchanged transcript.
                old = self.signatures.get(key)
                signature = (meeting.words, meeting.speakers, meeting.started_at_ms, meeting.origin)
                if old is not None and old[0] is meeting.words and old[1:] == signature[1:]:
                    continue
                ordered = sorted(meeting.words, key=lambda word: word.start_ms)
                turns = tuple(
                    SearchTurn(speaker, items, " ".join(w.text.replace(START, "").replace(END, "") for w in items))
                    for speaker, words in groupby(ordered, key=lambda w: w.speaker)
                    if (items := tuple(words))
                )
                names = {s.id: s.name.casefold() for s in meeting.speakers}
                rows = [(t.text, key, i, names.get(t.speaker, "")) for i, t in enumerate(turns)]
                fingerprint = hashlib.sha256(
                    json.dumps([rows, meeting.started_at_ms, meeting.origin], ensure_ascii=False).encode()
                ).hexdigest()
                self.turns[key] = turns
                self.signatures[key] = signature
                if stored.get(key) == fingerprint:
                    continue
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.executemany("INSERT INTO hits(body,key,turn,speaker) VALUES (?,?,?,?)", rows)
                self.db.execute(
                    "INSERT OR REPLACE INTO meetings VALUES (?,?,?,?)",
                    (key, fingerprint, meeting.started_at_ms, meeting.origin),
                )
        self.current = meetings

    def speakers(self):
        names = {}
        for meeting in self.current.values():
            for speaker in meeting.speakers:
                if speaker.provenance != "unnamed" and not re.fullmatch(r"Speaker \d+", speaker.name):
                    names.setdefault(speaker.name.casefold(), speaker.name)
        return tuple(names[key] for key in sorted(names))

    def query(self, query, *, speaker=None, date="any", own=False, now_ms, tz):
        query = query.strip()
        if not query or not any(char.isalnum() for char in query):
            return ()
        # All input is one quoted literal phrase; operators and quotes never
        # become query syntax. Parameters also cover every filter.
        params = ['"' + query.replace('"', '""') + '"']
        conditions = ["hits MATCH ?"]
        if speaker:
            conditions.append("hits.speaker = ?")
            params.append(speaker.casefold())
        if own:
            conditions.append("meetings.origin = 'own'")
        if bounds := date_range(date, now_ms, tz):
            conditions.append("meetings.started >= ? AND meetings.started < ?")
            params.extend(bounds)
        sql = """SELECT hits.key, hits.turn, highlight(hits,0,char(1),char(2))
                 FROM hits JOIN meetings ON meetings.key=hits.key WHERE """ + " AND ".join(conditions)
        sql += " ORDER BY meetings.started IS NULL, meetings.started DESC, meetings.key, CAST(hits.turn AS INTEGER)"
        result = []
        for key, turn_index, marked in self.db.execute(sql, params):
            self._check_cancelled()
            turn = self.turns[key][turn_index]
            # Seek the original timed item containing the first matching token.
            first = marked.index(START)
            end = 0
            position = turn.start_ms
            for word in turn.words:
                end += len(word.text) + 1
                if first < end:
                    position = word.start_ms
                    break
            result.append(
                SearchHit(
                    key,
                    turn_index,
                    marked,
                    position,
                    excerpt_markup(marked),
                    excerpt_markup(marked, selected=True),
                    excerpt_markup(marked, limit=800),
                )
            )
        return tuple(result)

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError("Search closed")

    def close(self):
        self.db.close()
