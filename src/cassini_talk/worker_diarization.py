# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional D24 step 3b inside either existing recognition worker."""

from __future__ import annotations

import contextlib
import json
import math
import time
from pathlib import Path

from .diarization import group_tracks
from .nemotron_model import MODEL_TAG, model_signature_matches
from .received import regular_input
from .sherpa_runtime import runtime_signature_matches

PARAMS = dict(minDurationOn=0.3, minDurationOff=0.5)
FORMAT = "cassini.speaker-turns.v1"
MIN_WARM_MEMORY_BYTES = 512 * 1024 * 1024


def available_memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """Return Linux MemAvailable in bytes, or None when /proc/meminfo is unavailable."""
    try:
        for line in meminfo.read_text(encoding="ascii", errors="ignore").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def has_memory_headroom(*, min_bytes: int = MIN_WARM_MEMORY_BYTES, mem_available=None) -> bool:
    avail = mem_available() if mem_available is not None else available_memory_bytes()
    return avail is None or avail >= min_bytes


class WarmDiarizer:
    """Bounded pre-verified Nemotron handle inside the background speech worker."""

    def __init__(self):
        self.model = None
        self.model_path: str | None = None
        self.runtime_path: str | None = None
        self.model_sig: tuple | None = None
        self.runtime_sig: tuple | None = None

    def _signatures_match(self, model_path: str, runtime_path: str, model_sig, runtime_sig) -> bool:
        if model_sig is not None and not model_signature_matches(model_path, model_sig):
            return False
        return runtime_sig is None or runtime_signature_matches(runtime_path, runtime_sig)

    def warm(self, options, *, statvfs=None, mem_available=None) -> bool:
        from .scratch_budget import live_recordings, shortfall

        if not isinstance(options, dict):
            self.close()
            return False
        model_path = options.get("modelPath")
        runtime_path = options.get("runtimePath")
        if not isinstance(model_path, str) or not model_path or not isinstance(runtime_path, str) or not runtime_path:
            self.close()
            return False
        if not has_memory_headroom(mem_available=mem_available):
            self.close()
            return False
        if shortfall(0, scratch=options.get("scratch"), statvfs=statvfs, recordings=live_recordings(options)):
            self.close()
            return False
        if (
            self.model is not None
            and self.model_path == model_path
            and self.runtime_path == runtime_path
            and self._signatures_match(model_path, runtime_path, self.model_sig, self.runtime_sig)
        ):
            return True
        self.close()
        try:
            from .nemotron import Nemotron

            instance = Nemotron(model_path, runtime_path)
        except Exception:
            return False
        self.model = instance
        self.model_path = model_path
        self.runtime_path = runtime_path
        self.model_sig = getattr(instance, "_model_sig", None)
        self.runtime_sig = getattr(instance, "_runtime_sig", None)
        return True

    def take(self, options):
        instance, self.model = self.model, None
        model_path, self.model_path = self.model_path, None
        runtime_path, self.runtime_path = self.runtime_path, None
        model_sig, self.model_sig = self.model_sig, None
        runtime_sig, self.runtime_sig = self.runtime_sig, None
        if instance is None:
            return None
        if (
            isinstance(options, dict)
            and model_path == options.get("modelPath")
            and runtime_path == options.get("runtimePath")
            and model_path is not None
            and runtime_path is not None
            and self._signatures_match(model_path, runtime_path, model_sig, runtime_sig)
        ):
            return instance
        with contextlib.suppress(Exception):
            instance.close()
        return None

    def close(self) -> None:
        instance, self.model = self.model, None
        self.model_path = None
        self.runtime_path = None
        self.model_sig = None
        self.runtime_sig = None
        if instance is not None:
            with contextlib.suppress(Exception):
                instance.close()


def valid_group(group):
    return (
        isinstance(group, dict)
        and isinstance(group.get("id"), str)
        and type(group.get("durationMs")) is int
        and group["durationMs"] >= 0
        and isinstance(group.get("inputSHA256"), str)
        and len(group["inputSHA256"]) == 64
        and isinstance(group.get("turns"), list)
        and len(group["turns"]) <= 100000
        and all(
            isinstance(t, dict)
            and all(type(t.get(k)) is int for k in ("speaker", "startMs", "endMs"))
            and t["speaker"] >= 0
            and 0 <= t["startMs"] < t["endMs"] <= group["durationMs"]
            for t in group["turns"]
        )
    )


def read_cache(path):
    if not path:
        return {}
    try:
        with regular_input(path) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            return {}
        value = json.loads(raw)
        if value.get("format") != FORMAT or value.get("model") != MODEL_TAG or value.get("params") != PARAMS:
            return {}
        groups = value["groups"]
        if not isinstance(groups, list) or len(groups) > 64 or not all(valid_group(g) for g in groups):
            return {}
        return {g["id"]: g for g in groups}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def run(tracks, options, notify, *, statvfs=None, diarization_store=None, warm_diarizer: WarmDiarizer | None = None):
    """Failure keeps D11. Results are streamed in bounded messages, never words."""
    from .diarization_audio import (
        LowDiskFallbackError,
        close_diarization_store,
        discard_staged_for_track,
        pop_staged_for_track,
        rejoin_with_staged,
    )
    from .scratch_budget import live_recordings, shortfall

    live = live_recordings(options)
    by_id = {t["id"]: t for t in tracks}
    cached = read_cache(options.get("storedTurnsPath"))
    model = None
    diagnostics, completed = [], []
    measured_seconds = measured_frames = 0
    staged_groups_reused = 0
    tail_frames_appended = 0
    warm_model_reused = False
    factor = options.get("realtimeFactor", 0.05)
    if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
        factor = 0.05
    try:
        groups = group_tracks(tracks, origin=options.get("origin", "own"))
        if diarization_store:
            single_eligible_ids = {
                group["tracks"][0] for group in groups if group["eligible"] and len(group["tracks"]) == 1
            }
            for track in tracks:
                if track["id"] not in single_eligible_ids:
                    discard_staged_for_track(diarization_store, track)
        for group in groups:
            if not group["eligible"]:
                continue
            identifier = group["id"]
            staged = (
                pop_staged_for_track(diarization_store, by_id[group["tracks"][0]])
                if len(group["tracks"]) == 1
                else None
            )
            try:
                staged_ms = (staged.total_frames * 1000 // 16000) if staged is not None and not staged.closed else 0
                # G14: too little disk for this group's temp audio keeps D11,
                # as any other failure does, without creating the file.
                if short := shortfall(
                    max(0, group["durationMs"] - staged_ms),
                    scratch=options.get("scratch"),
                    statvfs=statvfs,
                    recordings=live,
                ):
                    if staged is not None:
                        staged.close()
                    diagnostics.append(
                        dict(
                            code="diarization-kept-d11",
                            group=identifier,
                            reason="lowDisk",
                            message="Not enough disk space",
                            **short,
                        )
                    )
                    continue

                def heartbeat(group=group, identifier=identifier):
                    notify(
                        "diarizing",
                        groupId=identifier,
                        etaMinutes=max(1, math.ceil(group["durationMs"] * factor / 60000)),
                    )

                with rejoin_with_staged(
                    [by_id[t] for t in group["tracks"]],
                    staged=staged,
                    scratch=options.get("scratch"),
                    heartbeat=heartbeat,
                    check_full_budget=lambda group=group: shortfall(
                        group["durationMs"], scratch=options.get("scratch"), statvfs=statvfs, recordings=live
                    ),
                ) as (stream, frames, sha, staged_reused, tail_frames):
                    if staged_reused:
                        staged_groups_reused += 1
                        tail_frames_appended += tail_frames
                    duration = frames * 1000 // 16000
                    old = cached.get(identifier)
                    if old and old["inputSHA256"] == sha and old["durationMs"] == duration:
                        turns, reused = old["turns"], True
                    elif frames < 3200:
                        turns, reused = [], False
                    else:
                        if model is None and warm_diarizer is not None:
                            model = warm_diarizer.take(options)
                            if model is not None:
                                warm_model_reused = True
                        if model is None:
                            from .nemotron import Nemotron

                            model = Nemotron(options["modelPath"], options["runtimePath"])
                        import numpy as np

                        # Mapping stays owned by this block, and all C pointers
                        # are released before closing it. No full PCM copy.
                        samples = np.memmap(stream, dtype="<f4", mode="r", shape=(frames,))
                        last = time.monotonic()

                        def progress(_done, _total):
                            nonlocal last
                            if time.monotonic() - last >= 1:
                                heartbeat()
                                last = time.monotonic()

                        try:
                            result = model.infer(samples, progress)
                        finally:
                            samples._mmap.close()
                        measured_seconds += result["inferenceSeconds"]
                        measured_frames += frames
                        turns, reused = result["turns"], False
                value = dict(id=identifier, durationMs=duration, inputSHA256=sha, turns=turns)
                if not valid_group(value):
                    raise ValueError("Invalid diarization group")
                # The existing transport limits each reply to 1 MiB. Long turns
                # use separate messages, with an explicit final batch marker.
                for first in range(0, max(1, len(turns)), 512):
                    notify(
                        "diarized",
                        groupId=identifier,
                        durationMs=duration,
                        inputSHA256=sha,
                        turns=turns[first : first + 512],
                        first=first == 0,
                        final=first + 512 >= len(turns),
                        reused=reused,
                        etaMinutes=0,
                    )
                completed.append(identifier)
            except LowDiskFallbackError as low_disk:
                diagnostics.append(
                    dict(
                        code="diarization-kept-d11",
                        group=identifier,
                        reason="lowDisk",
                        message="Not enough disk space",
                        **low_disk.shortfall_info,
                    )
                )
            except Exception as error:
                if staged is not None:
                    staged.close()
                diagnostics.append(dict(code="diarization-kept-d11", group=identifier, message=str(error)))
    finally:
        if model is not None:
            model.close()
        if warm_diarizer is not None:
            warm_diarizer.close()
        close_diarization_store(diarization_store)
    return dict(
        model=MODEL_TAG,
        params=PARAMS,
        groupIds=completed,
        realtimeFactor=measured_seconds * 16000 / measured_frames if measured_frames else None,
        diagnostics=diagnostics,
        stagedGroupsReused=staged_groups_reused,
        warmModelReused=warm_model_reused,
        tailFramesAppended=tail_frames_appended,
    )


def estimate(tracks, options):
    if not options:
        return 0
    factor = options.get("realtimeFactor", 0.05)
    if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
        factor = 0.05
    return (
        sum(g["durationMs"] for g in group_tracks(tracks, origin=options.get("origin", "own")) if g["eligible"])
        * factor
        / 1000
    )


def complete(
    summary, tracks, options, notify, *, statvfs=None, diarization_store=None, warm_diarizer: WarmDiarizer | None = None
):
    from .diarization_audio import close_diarization_store

    if not options:
        close_diarization_store(diarization_store)
        if warm_diarizer is not None:
            warm_diarizer.close()
        return summary
    # If a native diarizer crashes after recognition, the parent still has
    # every word and this marker. It can finish the transcript with D11 names.
    notify("recognised", summary=summary, etaMinutes=1)
    try:
        summary["diarization"] = run(
            tracks,
            options,
            notify,
            statvfs=statvfs,
            diarization_store=diarization_store,
            warm_diarizer=warm_diarizer,
        )
    except Exception as error:
        summary.setdefault("diagnostics", []).append(dict(code="diarization-kept-d11", message=str(error)))
    finally:
        close_diarization_store(diarization_store)
        if warm_diarizer is not None:
            warm_diarizer.close()
    return summary
