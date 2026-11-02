# SPDX-License-Identifier: GPL-3.0-or-later
"""S-9 private, atomic voice storage. All IO methods run off GTK; snapshot and has_match are cached."""

import contextlib
import copy
import fcntl
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from .library import meeting_id
from .library_edits import transaction
from .meeting_store import LOCK as METADATA_LOCK
from .meeting_store import atomic_json
from .model_download import model_root
from .received import regular_input
from .speaker_edits import name_value
from .voices_logic import (
    DIMENSION,
    REVISION,
    UnsupportedVersion,
    cosine,
    decode_people,
    enrol,
    match,
    name_key,
    stale,
    unenrol,
)

LOG = logging.getLogger(__name__)
LOCK = threading.RLock()
LIMIT = 16 * 1024 * 1024
JOURNAL_LIMIT = 128 * 1024 * 1024


def _load(path, *, limit=None):
    limit = LIMIT if limit is None else limit
    with regular_input(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Saved voices file exceeds limit")
    return json.loads(raw, parse_constant=_invalid)


def _invalid(value):
    raise ValueError(f"Invalid saved voices number: {value}")


def _version(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] < 1:
        raise ValueError("Invalid saved voices version")
    if value["version"] > 1:
        raise UnsupportedVersion(value["version"])


def _vector(value):
    return (
        isinstance(value, list)
        and len(value) == DIMENSION
        and all(type(v) in (int, float) and math.isfinite(v) for v in value)
        and 0.99 < sum(float(v) * v for v in value) < 1.01
    )


def _key(directory):
    name = Path(directory).name
    if not meeting_id(Path(name)):
        raise ValueError("Saved voices require a full Library folder basename")
    return name


class VoiceStore:
    def __init__(self, root=None, changed=lambda: None):
        self.root = Path(root) if root is not None else model_root().parent / "voices"
        self.changed = changed
        self._snapshot = dict(ready=False, readOnly=False, voices=[], generation=0)
        self._snapshot_lock = threading.Lock()
        self._auto = set()
        self._library_keys = None

    def snapshot(self):
        with self._snapshot_lock:
            return copy.deepcopy(self._snapshot)

    def match_signature(self):
        with self._snapshot_lock:
            return frozenset(self._auto)

    def has_match(self, directory, speaker):
        with self._snapshot_lock:
            return (Path(directory).name, speaker) in self._auto

    def _publish(self, people, *, readonly=False):
        links, auto = self._links(people)
        self._meeting_lists(people, links)
        voices = sorted(people["voices"], key=lambda v: (-len(v["meetings"]), -v["seconds"], name_key(v["name"])))
        snapshot = dict(ready=True, readOnly=readonly, voices=copy.deepcopy(voices), generation=people["generation"])
        with self._snapshot_lock:
            changed = self._auto != auto or self._snapshot != snapshot
            self._auto, self._snapshot = auto, snapshot
        if changed:
            self.changed()

    @staticmethod
    def _meeting_lists(people, links):
        for voice in people["voices"]:
            keys = links.get(voice["id"], set())
            kept = [key for key in voice["meetings"] if key in keys]
            voice["meetings"] = kept + sorted(keys - set(kept))

    def _links(self, people, replacements=None, *, current_key=None):
        replacements = replacements or {}
        existing = {v["id"] for v in people["voices"]}
        paths = {"meetings/" + p.name for p in (self.root / "meetings").glob("*.json")}
        paths.update(k for k in replacements if k.startswith("meetings/"))
        links, auto = {}, set()
        for relative in sorted(paths):
            path = Path(relative)
            try:
                key = _key(path.stem)
                if self._library_keys is not None and key not in self._library_keys and key != current_key:
                    continue
                cache = replacements[relative] if relative in replacements else self._meeting(key, people["generation"])
            except (OSError, ValueError):
                continue
            if cache is None:
                continue
            for speaker, item in cache["speakers"].items():
                m = item.get("match", {})
                if m.get("voiceId") in existing and m.get("state") in ("auto", "confirmed"):
                    links.setdefault(m["voiceId"], set()).add(key)
                    auto.add((key, speaker))
        return links, auto

    def sync_library(self, keys):
        keys = frozenset(_key(key) for key in keys)
        with self._locked():
            self._library_keys = keys
            try:
                people = self._people()
            except UnsupportedVersion:
                self._publish(dict(voices=[], generation=0), readonly=True)
                return
            before = copy.deepcopy(people)
            links, _auto = self._links(people)
            self._meeting_lists(people, links)
            if people != before:
                atomic_json(self.root / "people.json", people)
            self._publish(people)

    @contextlib.contextmanager
    def _locked(self):
        with LOCK:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise ValueError("Saved voices directory must not be a symlink")
            meetings = self.root / "meetings"
            meetings.mkdir(mode=0o700, exist_ok=True)
            if meetings.is_symlink():
                raise ValueError("Saved voice meeting directory must not be a symlink")
            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._recover()
                yield
            finally:
                os.close(fd)

    def _read(self, path, validator, empty):
        try:
            value = _load(path)
            _version(value)
            validator(value)
            return value
        except FileNotFoundError:
            return copy.deepcopy(empty)
        except UnsupportedVersion:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            # Keep the original for diagnosis, never silently truncate it.
            stamp = int(time.time() * 1000)
            damaged = path.with_name(path.name + f".corrupt-{stamp}")
            while damaged.exists():
                stamp += 1
                damaged = path.with_name(path.name + f".corrupt-{stamp}")
            path.rename(damaged)
            LOG.warning("Ignoring damaged saved voices file %s", path)
            replacement = copy.deepcopy(empty)
            if path.name == "people.json":
                replacement["generation"] = time.time_ns()
                atomic_json(path, replacement)
            return replacement

    @staticmethod
    def _validate_people(value):
        if type(value["generation"]) is not int or value["generation"] < 0:
            raise ValueError("Invalid voice generation")
        voices = decode_people(json.dumps(value, allow_nan=False))
        seen = set()
        for voice in voices:
            if not isinstance(voice["id"], str) or not voice["id"].startswith("voice_") or voice["id"] in seen:
                raise ValueError("Invalid or duplicate saved voice id")
            seen.add(voice["id"])
            if name_value(voice["name"]) != voice["name"] or not voice["name"]:
                raise ValueError("Invalid saved name")
            if not isinstance(voice["model"], str) or len(voice["model"]) != 64:
                raise ValueError("Invalid saved model")
            if (
                type(voice["seconds"]) not in (int, float)
                or not math.isfinite(voice["seconds"])
                or voice["seconds"] < 0
            ):
                raise ValueError("Invalid confirmed speech")
            if any(type(voice[k]) is not int or voice[k] < 0 for k in ("createdAt", "updatedAt")):
                raise ValueError("Invalid saved timestamps")
            if not isinstance(voice["meetings"], list) or any(_key(k) != k for k in voice["meetings"]):
                raise ValueError("Invalid voice meeting keys")

    def _people(self):
        return self._read(self.root / "people.json", self._validate_people, dict(version=1, generation=0, voices=[]))

    @staticmethod
    def _validate_meeting(value):
        if value["model"] != REVISION or not isinstance(value["speakers"], dict):
            raise ValueError("Invalid meeting voice model")
        for speaker, item in value["speakers"].items():
            if (
                speaker == "me"
                or not isinstance(speaker, str)
                or not 1 <= len(speaker) <= 256
                or not _vector(item["embedding"])
            ):
                raise ValueError("Invalid meeting print")
            if (
                type(item["seconds"]) not in (int, float)
                or not math.isfinite(item["seconds"])
                or not 4 <= item["seconds"] <= 45
            ):
                raise ValueError("Invalid selected speech")
            windows = item.get("windows", [])
            if (
                not isinstance(windows, list)
                or len(windows) > 64
                or any(
                    not isinstance(w, dict)
                    or type(w.get("startMs")) is not int
                    or type(w.get("endMs")) is not int
                    or not 0 <= w["startMs"] < w["endMs"] <= w["startMs"] + 10000
                    for w in windows
                )
            ):
                raise ValueError("Invalid voice sample windows")
            m = item.get("match")
            if m is not None and (
                m.get("state") not in ("auto", "suggested", "confirmed", "none", "rejected")
                or m.get("voiceId") is not None
                and not isinstance(m["voiceId"], str)
                or m.get("score") is not None
                and (type(m["score"]) not in (int, float) or not math.isfinite(m["score"]))
            ):
                raise ValueError("Invalid saved match")

    def _meeting(self, key, generation):
        empty = dict(version=1, model=REVISION, generation=generation, speakers={})
        result = self._read(self.root / "meetings" / (key + ".json"), self._validate_meeting, empty)
        # The generation also makes interrupted Forget-everyone cleanup safe.
        return result if result.get("generation", generation) == generation else empty

    def _transaction(self, values, *, before_commit=None, current_key=None):
        for relative, value in values.items():
            if relative == "people.json":
                self._validate_people(value)
            elif value is not None:
                self._validate_meeting(value)
        if "people.json" in values:
            self._meeting_lists(
                values["people.json"], self._links(values["people.json"], values, current_key=current_key)[0]
            )
        old = {}
        for relative in values:
            path = self.root / relative
            old[relative] = _load(path) if path.exists() else None
        journal = dict(version=1, rollback=old)
        if (
            len(json.dumps(journal, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
            > JOURNAL_LIMIT
        ):
            raise ValueError("Saved voice edit exceeds the recovery size limit")
        atomic_json(self.root / ".transaction.json", journal, before_commit=before_commit)
        try:
            for relative, value in values.items():
                if value is None:
                    if before_commit is not None:
                        before_commit()
                    (self.root / relative).unlink(missing_ok=True)
                else:
                    atomic_json(self.root / relative, value, before_commit=before_commit)
        except BaseException:
            self._recover()
            raise
        (self.root / ".transaction.json").unlink()
        if current_key is not None and self._library_keys is not None:
            self._library_keys = self._library_keys | {current_key}

    def _recover(self):
        journal = self.root / ".transaction.json"
        if not journal.exists():
            return
        value = _load(journal, limit=JOURNAL_LIMIT)
        _version(value)
        # Never recover a transaction across an upgrade to an unknown format.
        for relative in value["rollback"]:
            path = Path(relative)
            if relative != "people.json" and not (
                path.parent == Path("meetings") and path.suffix == ".json" and _key(path.stem) == path.stem
            ):
                raise ValueError("Invalid private voice transaction")
            current = self.root / relative
            if current.exists():
                _version(_load(current))
        for relative, previous in value["rollback"].items():
            path = self.root / relative
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_json(path, previous)
        journal.unlink()

    def remove_meeting(self, directory):
        """D29 removes only this meeting's private prints; never unenrol a person."""
        key = _key(Path(directory).name)
        with self._locked():
            relative = "meetings/" + key + ".json"
            try:
                people = self._people()
            except UnsupportedVersion:
                # Explicit removal can discard this private cache without
                # interpreting or rewriting the newer people store.
                (self.root / relative).unlink(missing_ok=True)
                return
            self._transaction({relative: None, "people.json": people})
            if self._library_keys is not None:
                self._library_keys = self._library_keys - {key}
            self._publish(people)

    def refresh(self):
        with self._locked():
            try:
                people = self._people()
            except UnsupportedVersion:
                self._publish(dict(voices=[], generation=0), readonly=True)
            else:
                original = copy.deepcopy(people)
                self._meeting_lists(people, self._links(people)[0])
                if people != original:
                    atomic_json(self.root / "people.json", people)
                self._publish(people)
        return self.snapshot()

    def meeting(self, directory):
        key = _key(directory)
        with self._locked():
            people = self._people()
            result = self._meeting(key, people["generation"])
            self._publish(people)
            return result

    def preparation(self, directory):
        """Return the cache and whether extraction actually completed.

        An absent entry is evidence of short speech only after a successful
        extraction. Missing, damaged and obsolete caches are not that evidence.
        """
        key = _key(directory)
        with self._locked():
            people = self._people()
            path = self.root / "meetings" / (key + ".json")
            empty = dict(version=1, model=REVISION, generation=people["generation"], speakers={})
            value = self._read(path, self._validate_meeting, empty)
            prepared = path.is_file() and value.get("generation", people["generation"]) == people["generation"]
            return (value if prepared else empty), prepared

    def commit_job(
        self,
        directory,
        prints,
        generation,
        *,
        check_active=lambda: None,
        apply_names=None,
        auto_eligible=None,
        voice_sources=None,
    ):
        key = _key(directory)
        sources = voice_sources or {}
        with self._locked(), transaction(directory):
            people = self._people()
            if people["generation"] != generation:
                return {}
            previous = self._meeting(key, generation)
            # G12: a confirmed match, its embedding and rejections belong to the
            # voice they were made on. Carry them only while its stamp is equal.
            previous["speakers"] = {
                s: item for s, item in previous["speakers"].items() if item.get("voiceSource") == sources.get(s)
            }
            excluded = {s: item.get("rejected", []) for s, item in previous["speakers"].items()}
            for speaker, item in previous["speakers"].items():
                if item.get("match", {}).get("state") == "rejected":
                    excluded.setdefault(speaker, []).append(item["match"]["voiceId"])
            value = dict(version=1, model=REVISION, generation=generation, speakers=copy.deepcopy(prints))
            for speaker, item in value["speakers"].items():
                if speaker in sources:
                    item["voiceSource"] = dict(sources[speaker])
            self._validate_meeting(value)
            current_voices = {v["id"]: v for v in people["voices"] if not stale(v)}
            confirmed = {
                s: p
                for s, p in previous["speakers"].items()
                if s in prints
                and p.get("match", {}).get("state") == "confirmed"
                and p["match"].get("voiceId") in current_voices
            }
            occupied = {p["match"]["voiceId"] for p in confirmed.values()}
            eligible = set(prints) if auto_eligible is None else set(auto_eligible())
            matches = match(
                {s: p["embedding"] for s, p in prints.items() if s not in confirmed and s in eligible},
                [v for v in people["voices"] if v["id"] not in occupied],
                excluded=excluded,
            )
            # Explicit names/Clear may still receive a suggestion, but cannot
            # consume another speaker's automatic match or link this meeting.
            for speaker in prints.keys() - eligible - confirmed.keys():
                suggestion = match({speaker: prints[speaker]["embedding"]}, people["voices"], excluded=excluded)
                if speaker in suggestion:
                    matches[speaker] = dict(suggestion[speaker], state="suggested")
            for speaker, item in value["speakers"].items():
                item["match"] = matches.get(speaker, dict(voiceId=None, score=None, state="none"))
                if excluded.get(speaker):
                    item["rejected"] = excluded[speaker]
            value["speakers"].update(copy.deepcopy(confirmed))
            check_active()
            self._transaction(
                {"people.json": people, "meetings/" + key + ".json": value},
                before_commit=check_active,
                current_key=key,
            )
            self._publish(people)
            names = {v["id"]: v["name"] for v in people["voices"]}
            names = {s: names[m["voiceId"]] for s, m in matches.items() if m["state"] == "auto"}
            if apply_names is not None:
                check_active()
                apply_names(names)
            return names

    def remember(self, directory, speaker, name, *, voice_id=None, remember=True):
        """The name write is independent: a private-store error never loses it."""
        key, name = _key(directory), name_value(name)
        with self._locked():
            people = self._people()
            meeting = self._meeting(key, people["generation"])
            item = meeting["speakers"].get(speaker)
            if item is None:
                return False
            voices = {v["id"]: v for v in people["voices"]}
            old = item.get("match") or {}
            old_voice = voices.get(old.get("voiceId"))
            same = old_voice is not None and voice_id == old_voice["id"]
            if old_voice and not same and old.get("state") == "confirmed":
                reduced = unenrol(old_voice, item["embedding"])
                if reduced is None or reduced["count"] <= 0:
                    voices.pop(old_voice["id"])
                else:
                    voices[old_voice["id"]] = reduced
            if old_voice and not same and old.get("state") in ("confirmed", "auto", "suggested"):
                item.setdefault("rejected", []).append(old_voice["id"])
                item["match"] = dict(old, state="rejected")
            if name and remember and item["seconds"] >= 4:
                previous = voices.get(voice_id)
                if not (same and old.get("state") == "confirmed" and not stale(old_voice)):
                    voice = enrol(previous, name, REVISION, item["embedding"], item["seconds"], int(time.time() * 1000))
                    voice["meetings"] = list(dict.fromkeys((previous or {}).get("meetings", []) + [key]))
                    voices[voice["id"]] = voice
                else:
                    voice = old_voice
                from .voices_logic import cosine

                item["match"] = dict(
                    voiceId=voice["id"], score=cosine(item["embedding"], voice["mean"]), state="confirmed"
                )
                item["rejected"] = [identifier for identifier in item.get("rejected", []) if identifier != voice["id"]]
            people["voices"] = list(voices.values())
            self._transaction({"people.json": people, "meetings/" + key + ".json": meeting}, current_key=key)
            self._publish(people)
            return True

    def forget(self, identifier):
        with self._locked():
            people = self._people()
            people["voices"] = [v for v in people["voices"] if v["id"] != identifier]
            values = {"people.json": people}
            for path in (self.root / "meetings").glob("*.json"):
                meeting = self._meeting(_key(path.stem), people["generation"])
                changed = False
                for item in meeting["speakers"].values():
                    if item.get("match", {}).get("voiceId") == identifier:
                        item["match"] = dict(voiceId=None, score=None, state="none")
                        changed = True
                    if identifier in item.get("rejected", []):
                        item["rejected"].remove(identifier)
                        changed = True
                if changed:
                    values["meetings/" + path.name] = meeting
            self._transaction(values)
            self._publish(people)

    def forget_everyone(self):
        with self._locked():
            people = self._people()
            people = dict(version=1, generation=people["generation"] + 1, voices=[])
            atomic_json(self.root / "people.json", people)
            self._publish(people)
            # Generation was committed first. Even a failed cleanup cannot
            # revive matches or let an already-running job put prints back.
            for path in (self.root / "meetings").glob("*.json"):
                try:
                    _version(_load(path))
                except UnsupportedVersion:
                    continue  # Unknown-format files stay read-only, generation hides them.
                path.unlink()

    def recompute(self, directory, prints, affected, generation, *, check_active=lambda: None):
        """Replace affected prints without rematching a historical meeting."""
        key = _key(directory)
        with self._locked(), METADATA_LOCK:
            check_active()
            people = self._people()
            if people["generation"] != generation:
                return False
            meeting = self._meeting(key, generation)
            voices = {v["id"]: v for v in people["voices"]}
            original_voices = dict(voices)
            # A merged-away voice (or one with too little clean speech) has no
            # replacement. Keep its confirmed print and person for Undo.
            affected = sorted(set(affected))
            for speaker in affected:
                if speaker not in prints:
                    old = meeting["speakers"].get(speaker, {})
                    match = old.get("match", {})
                    if match.get("state") != "confirmed" or match.get("voiceId") not in voices:
                        meeting["speakers"].pop(speaker, None)
            replacements = {s: copy.deepcopy(prints[s]) for s in affected if s in prints}
            confirmed = {}
            for speaker in replacements:
                old = meeting["speakers"].get(speaker)
                if old and old.get("match", {}).get("state") == "confirmed":
                    previous = voices.get(old["match"]["voiceId"])
                    if previous is not None:
                        confirmed[speaker] = copy.deepcopy(previous)

            # Remove all replaced contributions first. Interleaving removal and
            # insertion corrupts a shared running mean and depends on set order.
            for speaker, previous in confirmed.items():
                current = voices.get(previous["id"])
                if current is not None:
                    old = meeting["speakers"][speaker]
                    reduced = unenrol(current, old["embedding"])
                    if reduced is not None and reduced["count"] > 0:
                        voices[previous["id"]] = reduced
                    else:
                        voices.pop(previous["id"], None)

            for speaker, item in replacements.items():
                previous = confirmed.get(speaker)
                old = meeting["speakers"].get(speaker, {})
                if old.get("rejected"):
                    item["rejected"] = copy.deepcopy(old["rejected"])
                old_match = old.get("match", {})
                old_voice = original_voices.get(old_match.get("voiceId"))
                item["match"] = dict(voiceId=None, score=None, state="none")
                if old_voice is not None:
                    item["match"] = dict(old_match)
                if previous is not None:
                    # Preserve the confirmed identity, not an automatic match.
                    voice = enrol(
                        voices.get(previous["id"]),
                        previous["name"],
                        REVISION,
                        item["embedding"],
                        item["seconds"],
                        int(time.time() * 1000),
                    )
                    voice["id"] = previous["id"]
                    voice["createdAt"] = previous["createdAt"]
                    # Unenrol keeps confirmed seconds. Replacement adds the
                    # newly selected speech, just as a new enrolment does.
                    if previous["id"] not in voices:
                        voice["seconds"] += previous["seconds"]
                    voice["meetings"] = list(dict.fromkeys(previous.get("meetings", []) + [key]))
                    voices[voice["id"]] = voice
                    item["match"] = dict(voiceId=voice["id"], score=1.0, state="confirmed")
                meeting["speakers"][speaker] = item
            for item in replacements.values():
                current_voice = voices.get(item["match"].get("voiceId"))
                if item["match"]["state"] != "confirmed" and current_voice is not None and not stale(current_voice):
                    item["match"]["score"] = cosine(item["embedding"], current_voice["mean"])
            self._validate_meeting(meeting)
            people["voices"] = list(voices.values())
            self._transaction(
                {"people.json": people, "meetings/" + key + ".json": meeting},
                before_commit=check_active,
                current_key=key,
            )
            self._publish(people)
            return True


def sample_window(cache, voice_id):
    windows = [
        w
        for item in cache["speakers"].values()
        if item.get("match", {}).get("voiceId") == voice_id and item["match"].get("state") in ("auto", "confirmed")
        for w in item.get("windows", [])
    ]
    if not windows:
        return None
    chosen = max(windows, key=lambda w: w["endMs"] - w["startMs"])
    return chosen["startMs"], min(chosen["endMs"], chosen["startMs"] + 10000)
