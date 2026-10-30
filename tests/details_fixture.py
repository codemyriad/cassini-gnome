# SPDX-License-Identifier: CC0-1.0
"""Synthetic D28 review meetings: sparse PCM, no personal speech or prints."""

import json
import struct
from pathlib import Path


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def build_library(root):
    root = Path(root)
    meetings = {}
    for index, (key, title, source, duration) in enumerate(
        [
            ("fallback", "Design review · Google Meet", "chromium", 2825000),
            ("microphone", "A thought after the meeting", "gnome", 83000),
            ("no-events", "An older recording", "gnome", 198000),
        ],
        1,
    ):
        identifier = f"d2800000-0000-4000-8000-{index:012d}"
        folder = root / ("Recording-" + identifier)
        folder.mkdir(parents=True)
        session = dict(
            version=1,
            id=identifier,
            title=title,
            source=source,
            state="completed",
            createdAt="2026-10-29T09:00:00Z",
            tracks=[],
        )

        def track(identifier, kind, start, end, name, *, dc=0, session=session, folder=folder):
            rate = 8000
            frames = (end - start) * rate // 1000
            size = frames * 2
            filename = f"track-{identifier}.wav"
            with (folder / filename).open("wb") as stream:
                stream.write(
                    struct.pack(
                        "<4sI4s4sIHHIIHH4sI",
                        b"RIFF",
                        size + 36,
                        b"WAVE",
                        b"fmt ",
                        16,
                        1,
                        1,
                        rate,
                        rate * 2,
                        2,
                        16,
                        b"data",
                        size,
                    )
                )
                if dc:
                    stream.write(struct.pack("<h", dc) * min(rate, frames))
                stream.truncate(44 + size)
            row = dict(
                id=identifier,
                kind=kind,
                file=filename,
                label=name,
                labelSource="device" if kind in ("microphone", "system") else "browser",
                sampleRate=rate,
                channels=1,
                frames=frames,
                startOffsetMs=start,
                endOffsetMs=end,
                ended=True,
            )
            session["tracks"].append(row)
            return row

        mic = track("microphone", "microphone", 0, duration, "Built-in Audio", dc=-2060 if key == "microphone" else 0)
        if key == "fallback":
            for i in range(3):
                row = track(f"remote-{i}", "remote", 0, 1870000, "Remote audio")
                row.update(gapFrames=0, droppedFrames=0)
            system = track("system", "system", 1870000, duration, "The others")
            system.update(gapFrames=4000, droppedFrames=0)
            mic.update(gapFrames=0, droppedFrames=0)
            session.update(
                problemKind="gap",
                events=[
                    dict(type="othersSource", atMs=0, source="browser", reason="start"),
                    dict(type="selection", atMs=0, me=True, others=True, reason="start"),
                    dict(type="othersSource", atMs=1870000, source="system", reason="lost"),
                ],
            )
        elif key == "microphone":
            mic.update(gapFrames=0, droppedFrames=0)
            # A reader-only fixture for an already-present optional end record.
            # No app, schema or protocol writer starts producing this event.
            session["events"] = [
                dict(type="selection", atMs=0, me=True, others=False, reason="start"),
                dict(type="end", atMs=duration, reason="user"),
            ]
        write_json(folder / "session.json", session)
        metadata = dict(
            version=1,
            revision=0,
            id=identifier,
            title=title,
            titleSource="user",
            transcript=dict(
                status="done",
                reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1"),
                model="whistle",
            ),
            speakers=[dict(id="me", name="Me", provenance="source", colourIndex=0)],
            notes=[],
            etaMinutes=None,
            error=None,
            quoteOverride=None,
            received=None,
        )
        if key == "microphone":
            metadata["recordingDetails"] = dict(
                writingDown=dict(
                    wallDurationMs=4217,
                    resumed=False,
                    speechSegments=1,
                    speakerSeparation=dict(status="skipped", reason="modelsNotInstalled"),
                )
            )
        write_json(folder / "meeting.json", metadata)
        write_json(
            folder / "transcript.json",
            dict(
                format="cassini.words.v1",
                language="en",
                items=[dict(startMs=1000, endMs=2000, text="This is a synthetic design fixture.", speaker="me")],
            ),
        )
        meetings[key] = folder
    return meetings


def build_many_spans(root, template):
    """Forty source changes: long timelines and a real scrollable facts area."""
    import wave

    identifier = "d2800000-0000-4000-8000-000000000004"
    folder = Path(root) / ("Recording-" + identifier)
    folder.mkdir()
    session = dict(
        version=1,
        id=identifier,
        title="Many source changes",
        source="gnome",
        state="completed",
        createdAt="2026-10-29T09:00:00Z",
        tracks=[],
        events=[],
    )
    for index in range(20):
        for kind in ("microphone", "system"):
            start = index * 2000 + (1000 if kind == "system" else 0)
            track = f"{kind}-{index}"
            filename = f"track-{track}.wav"
            with wave.open(str(folder / filename), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(bytes(16000))
            session["tracks"].append(
                dict(
                    id=track,
                    file=filename,
                    kind=kind,
                    label="Generated input",
                    labelSource="device",
                    sampleRate=8000,
                    channels=1,
                    frames=8000,
                    startOffsetMs=start,
                    endOffsetMs=start + 1000,
                    ended=True,
                    gapFrames=0,
                    droppedFrames=0,
                )
            )
        session["events"].extend(
            [
                dict(type="selection", atMs=index * 2000, me=True, others=False, reason="user"),
                dict(type="selection", atMs=index * 2000 + 1000, me=False, others=True, reason="user"),
            ]
        )
    metadata = json.loads((template / "meeting.json").read_text())
    metadata.update(id=identifier, title=session["title"], transcript=None)
    write_json(folder / "session.json", session)
    write_json(folder / "meeting.json", metadata)
    return folder
