# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuildable, worker-owned transcript FTS. Nothing is written into the Library."""

import hashlib
import json
import os
import re
import sqlite3
import stat
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .meeting_text import reading_turns
from .transcript import row_text

START, END = "\x01", "\x02"


@dataclass(frozen=True)
class SearchTurn:
    speaker: str
    words: tuple
    text: str
    items: tuple  # displayed atoms, including folded replies
    offsets: tuple = ()  # character offsets in text, including chip names

    @property
    def start_ms(self):
        return self.words[0].start_ms


def search_turns(words, gaps, speaker_name=lambda speaker: speaker):
    """Index the same reading paragraphs and inline replies as the reader."""
    for row in reading_turns(words, gaps):
        raw, spans = row_text(row, speaker_name)
        # Control delimiters belong only to the FTS highlighter.
        shown = raw.replace(START, "").replace(END, "")
        # Removing delimiters also shifts subsequent atom offsets.
        removed = [i for i, char in enumerate(raw) if char in (START, END)]
        offsets = tuple(offset - bisect_left(removed, offset) for _w, _v, offset in spans)
        yield SearchTurn(
            row.speaker,
            tuple(w for w, _v, _o in spans),
            shown,
            tuple(v.replace(START, "").replace(END, "") for _w, v, _o in spans),
            offsets,
        )


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
        self.db = self._open_database()
        if cancelled is not None:
            self.db.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
        self.current = {}
        self.turns = {}
        self.signatures = {}

    def _open_database(self):
        for attempt in range(2):
            # SQLite follows final symlinks. Precreate privately with no-follow,
            # and reject unsafe auxiliary files before SQLite can open them.
            for path in (
                self.path,
                *(self.path.with_name(self.path.name + suffix) for suffix in ("-journal", "-wal", "-shm")),
            ):
                if path.is_symlink():
                    raise ValueError("Search cache files must not be symbolic links")
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Search cache must be a private regular file")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            db = sqlite3.connect(self.path)
            try:
                db.execute("PRAGMA secure_delete=ON")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Corrupt search cache")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS meetings (
                        key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, started INTEGER, origin TEXT NOT NULL
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS hits USING fts5(
                        body, key UNINDEXED, turn UNINDEXED, speaker UNINDEXED, tokenize='unicode61'
                    );
                """)
                # quick_check omits FTS shadow-table consistency.
                db.execute("INSERT INTO hits(hits) VALUES('integrity-check')")
                db.commit()
                return db
            except (sqlite3.DatabaseError, ValueError) as error:
                db.close()
                code = getattr(error, "sqlite_errorcode", sqlite3.SQLITE_CORRUPT) & 0xFF
                if attempt or code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_ERROR):
                    raise
                self.path.unlink()
        raise AssertionError("Unreachable cache initialization")

    def sync(self, snapshot):
        meetings = {m.key: m for m in snapshot.meetings} if not snapshot.unavailable else {}
        stored = dict(self.db.execute("SELECT key, fingerprint FROM meetings"))
        next_turns, next_signatures = dict(self.turns), dict(self.signatures)
        with self.db:
            for key in stored.keys() - meetings.keys():
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.execute("DELETE FROM meetings WHERE key = ?", (key,))
                next_turns.pop(key, None)
                next_signatures.pop(key, None)
            for key, meeting in meetings.items():
                self._check_cancelled()
                # Scanner keeps immutable word tuples alive. Polling capture levels
                # never hashes or rewrites an unchanged transcript.
                old = self.signatures.get(key)
                signature = (meeting.words, meeting.speakers, meeting.started_at_ms, meeting.origin, meeting.turn_gaps)
                if old is not None and old[0] is meeting.words and old[1:] == signature[1:]:
                    continue
                labels = {s.id: s.name for s in meeting.speakers}
                turns = tuple(
                    search_turns(
                        meeting.words, meeting.turn_gaps, lambda speaker, labels=labels: labels.get(speaker, speaker)
                    )
                )
                names = {s.id: s.name.casefold() for s in meeting.speakers}
                rows = []
                for i, turn in enumerate(turns):
                    # Unfiltered queries index the whole rendered paragraph once.
                    spoken = [" "] * len(turn.text)
                    for value, offset in zip(turn.items, turn.offsets, strict=True):
                        spoken[offset : offset + len(value)] = value
                    rows.append(("".join(spoken), key, i, ""))
                    # Filtered queries match ONLY that speaker's atoms. Retain
                    # widths so highlight offsets still address the full prose,
                    # including chip names and other speakers' surrounding words.
                    scopes = {}
                    for word, shown, offset in zip(turn.words, turn.items, turn.offsets, strict=True):
                        name = names.get(word.speaker)
                        if name:
                            if name not in scopes:
                                scopes[name] = [" "] * len(turn.text)
                            scoped = scopes[name]
                            scoped[offset : offset + len(shown)] = shown
                    rows.extend(("".join(scoped), key, i, name) for name, scoped in scopes.items())
                fingerprint = hashlib.sha256(
                    json.dumps([rows, meeting.started_at_ms, meeting.origin], ensure_ascii=False).encode()
                ).hexdigest()
                next_turns[key] = turns
                next_signatures[key] = signature
                if stored.get(key) == fingerprint:
                    continue
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.executemany("INSERT INTO hits(body,key,turn,speaker) VALUES (?,?,?,?)", rows)
                self.db.execute(
                    "INSERT OR REPLACE INTO meetings VALUES (?,?,?,?)",
                    (key, fingerprint, meeting.started_at_ms, meeting.origin),
                )
        self.turns, self.signatures = next_turns, next_signatures
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
        conditions.append("hits.speaker = ?")
        params.append(speaker.casefold() if speaker else "")
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
            # Both projections index only spoken atoms, never generated chip
            # labels. Restore visible context without moving timed offsets.
            chars = iter(turn.text)
            marked = "".join(char if char in (START, END) else next(chars) for char in marked)
            # Seek the original timed item containing the first matching token.
            first = marked.index(START)
            position = turn.start_ms
            for word, value, offset in zip(turn.words, turn.items, turn.offsets, strict=True):
                if offset <= first < offset + len(value):
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
