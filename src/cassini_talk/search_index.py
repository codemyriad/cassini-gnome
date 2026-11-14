# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuildable, worker-owned transcript FTS. Nothing is written into the Library."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .meeting_text import reading_turns
from .tokens import search_hit_colours
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
    speaker: str = ""
    title_markup: str = ""


def title_match_query(value):
    """Android/iOS terms: independent prefix phrases, joined with AND.

    Whitespace separates phrases. Punctuation inside a term separates adjacent
    tokens; only that phrase's last token is a prefix. Keep combining marks
    attached to letters/digits, and never interpret typed operators as syntax.
    FTS5 puts the prefix star outside the quoted phrase (FTS4 puts it inside).
    """
    phrases = []
    for term in value.split():
        tokens, token = [], []
        for char in term:
            if char.isalpha() or char.isdecimal() or (token and unicodedata.category(char).startswith("M")):
                token.append(char)
            elif token:
                tokens.append("".join(token))
                token.clear()
        if token:
            tokens.append("".join(token))
        if tokens:
            phrases.append('"' + " ".join(tokens) + '"*')
    return " AND ".join(phrases) or None


def marked_markup(value, *, selected=False, dark=False):
    value = escape(value)
    bg, fg = search_hit_colours(dark=dark, selected=selected)
    return value.replace(START, f'<span background="{bg}" foreground="{fg}" weight="semibold">').replace(END, "</span>")


def excerpt_markup(marked, *, limit=220, selected=False, dark=False):
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
        output += marked_markup(START + value + END if active else value, selected=selected, dark=dark)
    return output + ("…" if hi < len(plain) else "")


def calendar_range(choice, now_ms, tz):
    if choice == "any":
        return None
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    today = datetime.fromtimestamp(now_ms / 1000, zone).date()
    if choice == "year":
        # D99: from local midnight on 1 January to the end of today.
        return today.replace(month=1, day=1), today + timedelta(days=1)
    days = {"today": 1, "week": 7, "month": 30}[choice]
    return today - timedelta(days=days - 1), today + timedelta(days=1)


def date_range(choice, now_ms, tz):
    dates = calendar_range(choice, now_ms, tz)
    if dates is None:
        return None
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return tuple(int(datetime.combine(day, time.min, zone).timestamp() * 1000) for day in dates)


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
                columns = {row[1] for row in db.execute("PRAGMA table_info(meetings)")}
                for column, kind in (("started_local", "TEXT"), ("sort_started", "INTEGER")):
                    if column not in columns:
                        db.execute(f"ALTER TABLE meetings ADD COLUMN {column} {kind}")
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS titles USING fts5(body, key UNINDEXED, tokenize='unicode61')"
                )
                # quick_check omits FTS shadow-table consistency.
                db.execute("INSERT INTO hits(hits) VALUES('integrity-check')")
                db.execute("INSERT INTO titles(titles) VALUES('integrity-check')")
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
        stored_titles = dict(self.db.execute("SELECT key, body FROM titles"))
        next_turns, next_signatures = dict(self.turns), dict(self.signatures)
        with self.db:
            for key in stored.keys() - meetings.keys():
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.execute("DELETE FROM titles WHERE key = ?", (key,))
                self.db.execute("DELETE FROM meetings WHERE key = ?", (key,))
                next_turns.pop(key, None)
                next_signatures.pop(key, None)
            for key, meeting in meetings.items():
                self._check_cancelled()
                title = meeting.title.replace(START, "").replace(END, "")
                if stored_titles.get(key) != title:
                    self.db.execute("DELETE FROM titles WHERE key = ?", (key,))
                    self.db.execute("INSERT INTO titles(body,key) VALUES (?,?)", (title, key))
                # Scanner keeps immutable word tuples alive. Polling capture levels
                # never hashes or rewrites an unchanged transcript.
                old = self.signatures.get(key)
                signature = (
                    meeting.words,
                    meeting.speakers,
                    meeting.display_start,
                    meeting.created_at_ms,
                    meeting.origin,
                    meeting.turn_gaps,
                )
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
                    json.dumps(
                        [rows, meeting.display_start, meeting.sort_time, meeting.origin], ensure_ascii=False
                    ).encode()
                ).hexdigest()
                next_turns[key] = turns
                next_signatures[key] = signature
                if stored.get(key) == fingerprint:
                    continue
                self.db.execute("DELETE FROM hits WHERE key = ?", (key,))
                self.db.executemany("INSERT INTO hits(body,key,turn,speaker) VALUES (?,?,?,?)", rows)
                self.db.execute(
                    "INSERT OR REPLACE INTO meetings(key,fingerprint,started,origin,started_local,sort_started) VALUES (?,?,?,?,?,?)",
                    (
                        key,
                        fingerprint,
                        meeting.started_at_ms,
                        meeting.origin,
                        meeting.recorded_at_local if meeting.started_at_ms is None else None,
                        meeting.sort_time,
                    ),
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

    def query(self, query, *, speaker=None, date="any", own=False, starred=None, dark=False, now_ms, tz):
        query = query.strip()
        if not query or not any(char.isalnum() for char in query):
            return ()
        # All input is one quoted literal phrase; operators and quotes never
        # become query syntax. Parameters also cover every filter.
        phrase = '"' + query.replace('"', '""') + '"'
        filters, filter_params = [], []
        if own:
            filters.append("meetings.origin = 'own'")
        if bounds := date_range(date, now_ms, tz):
            filters.append(
                "((meetings.started >= ? AND meetings.started < ?) OR "
                "(meetings.started IS NULL AND meetings.started_local >= ? AND meetings.started_local < ?))"
            )
            filter_params.extend(bounds)
            filter_params.extend(day.isoformat() + "T00:00:00" for day in calendar_range(date, now_ms, tz))
        conditions = ["hits MATCH ?", "hits.speaker = ?", *filters]
        params = [phrase, speaker.casefold() if speaker else "", *filter_params]
        sql = """SELECT hits.key, hits.turn, highlight(hits,0,char(1),char(2))
                 FROM hits JOIN meetings ON meetings.key=hits.key WHERE """ + " AND ".join(conditions)
        sql += " ORDER BY meetings.sort_started IS NULL, meetings.sort_started DESC, meetings.key, CAST(hits.turn AS INTEGER)"
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
            matched_speaker = turn.speaker
            for word, value, offset in zip(turn.words, turn.items, turn.offsets, strict=True):
                if offset <= first < offset + len(value):
                    position = word.start_ms
                    matched_speaker = word.speaker
                    break
            result.append(
                SearchHit(
                    key,
                    turn_index,
                    marked,
                    position,
                    excerpt_markup(marked, dark=dark),
                    excerpt_markup(marked, selected=True, dark=dark),
                    excerpt_markup(marked, limit=800, dark=dark),
                    matched_speaker,
                )
            )
        title_sql = """SELECT titles.key, highlight(titles,0,char(1),char(2))
                       FROM titles JOIN meetings ON meetings.key=titles.key WHERE """
        title_sql += " AND ".join(["titles MATCH ?", *filters])
        titles = {}
        if title_expression := title_match_query(query):
            for key, marked in self.db.execute(title_sql, [title_expression, *filter_params]):
                self._check_cancelled()
                titles[key] = marked_markup(marked, dark=dark)
        spoken_keys = {hit.key for hit in result}
        result = [replace(hit, title_markup=titles.get(hit.key, "")) for hit in result]
        for key in titles.keys() - spoken_keys:
            # D14 filters by the matched turn's speaker. Untimed titles do
            # not acquire attribution from the meeting's participant list.
            if speaker:
                continue
            turns = self.turns[key]
            preview = turns[0].text if turns else ""
            # A title has no timed word or speaker. Start at zero and keep it
            # outside the spoken-word index and its word-to-seek mapping.
            result.append(
                SearchHit(
                    key,
                    -1,
                    "",
                    0,
                    excerpt_markup(preview, dark=dark),
                    excerpt_markup(preview, selected=True, dark=dark),
                    "",
                    title_markup=titles[key],
                )
            )
        if starred is not None:
            # L-35: per-device stars are not in the index; AND them here.
            result = [hit for hit in result if hit.key in starred]
        result.sort(
            key=lambda hit: (
                self.current[hit.key].sort_time is None,
                -(self.current[hit.key].sort_time or 0),
                hit.key,
                hit.turn,
            )
        )
        return tuple(result)

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError("Search closed")

    def close(self):
        self.db.close()
