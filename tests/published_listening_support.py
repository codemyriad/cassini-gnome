# SPDX-License-Identifier: GPL-3.0-or-later
"""A real root Opus/portable transcript, without the optional mixer stack."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from cassini_talk import portable
from cassini_talk.library_v2 import edit, xschema
from cassini_talk.portable_writer import MANIFEST_MIME, chunk_set


def published_opus(root, *, merges=()):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    generator_path = Path(__file__).resolve().parents[1] / "ux/library-vectors/assets/build.py"
    spec = importlib.util.spec_from_file_location("listening_opus_fixture", generator_path)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = root
    generator.build("published.opus", 16000, title="Published listening fixture", portable=True)
    path = root / "published.opus"
    metadata = dict(
        version=1,
        id=str(uuid4()),
        revision=0,
        namePrefix="2026-11-07--12.00-",
        title=None,
        titleSource="meeting-window",
        notes=[],
        speakers=[
            dict(id="silvio", name="Marta", provenance="user", colourIndex=0),
            dict(id="guest", name="Dario", provenance="recognised", colourIndex=1),
        ],
        speakerMerges=list(merges),
        transcript=dict(status="done", reference=dict(kind="embedded", file=path.name, transcriptId="main")),
        etaMinutes=None,
        error=None,
        quoteOverride=None,
        received=None,
    )
    manifest = portable.read_meeting(str(path)).manifest
    manifest["speakers"] = [{"id": speaker["id"], "label": speaker["name"]} for speaker in metadata["speakers"]]
    manifest["x"] = {
        xschema.DOMAIN: xschema.private_data(metadata, origin="Recording", automatic_title=manifest["meeting"]["title"])
    }
    assert xschema.valid(manifest["x"][xschema.DOMAIN])
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    stage = path.with_suffix(".stage")
    edit.rewrite(path, stage, edit.edited_fields(fields, tags), vendor)
    edit.verify(stage)
    stage.replace(path)
    return path
