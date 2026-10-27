# SPDX-License-Identifier: GPL-3.0-or-later
"""Nonblocking, no-follow regular-file reads and disposable stat-keyed caches."""

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def basename(name):
    if not isinstance(name, str) or name in ("", ".", "..") or Path(name).name != name or "\\" in name or "\0" in name:
        raise ValueError("unsafe-reference")
    return name


def key(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@contextmanager
def regular(directory_fd, name):
    descriptor = os.open(
        basename(name), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not-a-regular-file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream, info
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ReadFailure:
    code: str


class FileCache:
    """Owned by one scanner; its lock serializes cache access across worker calls."""

    def __init__(self):
        self.entries = {}

    def get(self, directory_fd, path, kind, loader):
        info = os.stat(basename(path.name), dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not-a-regular-file")
        cache_key = (path, kind)
        cached = self.entries.get(cache_key)
        if cached and cached[0] == key(info):
            value = cached[1]
        else:
            with regular(directory_fd, path.name) as (stream, actual):
                try:
                    value = loader(stream, actual)
                except ValueError as error:
                    value = ReadFailure(str(error))
                self.entries[cache_key] = key(actual), value
        if isinstance(value, ReadFailure):
            raise ValueError(value.code)
        return value

    def forget(self, directory):
        self.entries = {k: v for k, v in self.entries.items() if k[0].parent != directory}
