# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact display heights while replacement geometry builds offscreen."""


class DisplayPrefix:
    """A measured prefix with logarithmic corrections for displayed rows.

    The base prefix stays intact. A Fenwick tree accumulates height changes,
    so refreshing visible rows never walks the unrendered transcript suffix.
    """

    def __init__(self, prefix, rows):
        self._base = list(prefix)
        self._changes = [0] * (rows + 1)

    def __len__(self):
        return len(self._base)

    def _delta(self, index):
        total = 0
        while index:
            total += self._changes[index]
            index -= index & -index
        return total

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._base[index] + self._delta(index)

    def append(self, total):
        self._base.append(total - self._delta(len(self._base)))

    def set_height(self, row, height):
        delta = height - (self[row + 1] - self[row])
        if not delta:
            return
        index = row + 1
        while index < len(self._changes):
            self._changes[index] += delta
            index += index & -index
