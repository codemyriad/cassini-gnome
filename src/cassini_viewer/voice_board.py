# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic private S-9 data for isolated UX boards only; no real voices."""

import os
from uuid import UUID


def seed(app, meeting):
    if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("Voice board fixtures require the private screenshot harness")
    from .meeting_store import atomic_json
    from .voices_logic import REVISION

    root = app.voice_store.root
    app.voice_store.refresh()
    people = []
    for index, (name, count, seconds) in enumerate(
        [("Marta", 31, 24 * 60 + 18), ("Luca", 14, 19 * 60 + 6), ("Giulia", 9, 8 * 60 + 42), ("Nina", 2, 4 * 60 + 8)]
    ):
        mean = [float(i == index) for i in range(512)]
        keys = [meeting.directory.name] + [
            "Recording-" + str(UUID(int=1000 + 100 * index + n)) for n in range(count - 1)
        ]
        people.append(
            dict(
                id="voice_" + str(UUID(int=2000 + index)),
                name=name,
                model=REVISION if name != "Nina" else "0" * 64,
                dim=512,
                mean=mean,
                count=count,
                seconds=seconds,
                meetings=keys,
                createdAt=1793100000000,
                updatedAt=1793107080000,
            )
        )
    atomic_json(root / "people.json", dict(version=1, generation=0, voices=people))
    prints = {
        "guest": dict(embedding=people[2]["mean"], seconds=8, windows=[dict(startMs=192000, endMs=200000)]),
        "marta": dict(embedding=people[0]["mean"], seconds=8, windows=[dict(startMs=0, endMs=8000)]),
    }
    app.voice_store.commit_job(meeting.directory, prints, 0)
    # For the sample page fixture all current profiles point to a playable
    # window; this is fixture data and never reaches the shipped Library.
    cache = app.voice_store.meeting(meeting.directory)
    cache["speakers"]["guest"]["match"] = dict(voiceId=None, score=None, state="none")
    for index, person in enumerate(people):
        cache["speakers"]["board-" + str(index)] = dict(
            embedding=person["mean"],
            seconds=8,
            windows=[dict(startMs=0, endMs=8000)],
            match=dict(voiceId=person["id"], score=1.0, state="confirmed"),
        )
    atomic_json(root / "meetings" / (meeting.directory.name + ".json"), cache)
    app.voice_store.refresh()
