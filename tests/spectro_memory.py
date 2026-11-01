# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned render-state accounting; shared models/pixel backing counted once."""

import sys


def render_state_bytes(widgets):
    seen = set()

    def size(value):
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        result = sys.getsizeof(value)
        if isinstance(value, dict):
            result += sum(size(key) + size(item) for key, item in value.items())
        elif isinstance(value, (list, tuple)):
            result += sum(map(size, value))
        elif type(value).__module__ in ("cassini_talk.spectrogram_strip", "cassini_talk.spectro_view_model"):
            result += size(vars(value))
        elif type(value).__module__ == "gi.repository.GLib" and type(value).__name__ == "Bytes":
            # GdkMemoryTexture retains this backing; it does not own a second
            # copy. Driver staging and GTK renderer allocations are separate.
            result += value.get_size()
        return result

    return size(list(widgets))


def source_digest():
    """Bind every report to the producer, model, renderer and UI source bytes."""
    import hashlib
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = sorted((root / "src/cassini_talk").glob("*.py")) + [root / "src/style.css"]
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    resource = Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource"
    digest.update(resource.read_bytes())
    digest.update((root / "src/native/spectro.c").read_bytes())
    digest.update((Path(os.environ["CASSINI_TEST_BUILD"]) / "src/libcassini-spectro.so").read_bytes())
    return digest.hexdigest()
