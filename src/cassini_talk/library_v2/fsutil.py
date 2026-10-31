# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable filesystem steps for Library v2 (S-12 §4.6, §7).

rename_noreplace() is renameat2(RENAME_NOREPLACE) through ctypes: the libc wrapper
when it exists (glibc >= 2.28), else the raw syscall on the architectures listed
below. Only when the kernel or the filesystem lacks the flag (ENOSYS, EINVAL,
EOPNOTSUPP: old kernels, some FUSE/network filesystems) does it fall back to
link() + unlink(). That fallback never replaces a file either (link() fails with
EEXIST), but it is not one atomic step: a crash between the two calls leaves the
stage and the published name as two links to one inode, which recovery resolves
by comparing inodes (publish.resume).
"""

import ctypes
import errno
import os
import platform
from pathlib import Path

AT_FDCWD = -100
RENAME_NOREPLACE = 1
# renameat2 syscall numbers, used only when libc has no wrapper (older musl).
SYSCALLS = {"x86_64": 316, "aarch64": 276, "riscv64": 276, "ppc64le": 357, "s390x": 347}
UNSUPPORTED = (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP)
_native = None


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path, data):
    """Replace `path` with `data` atomically and durably: sibling .partial, fsync, rename, fsync dir."""
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def _renameat2():
    """A callable (old, new) -> (result, errno) for renameat2 with RENAME_NOREPLACE, or None."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is not None:
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int

        def call(old, new):
            result = function(AT_FDCWD, old, AT_FDCWD, new, RENAME_NOREPLACE)
            return result, ctypes.get_errno() if result else 0

        return call
    number = SYSCALLS.get(platform.machine())
    if number is None:
        return None
    syscall = libc.syscall
    syscall.restype = ctypes.c_long

    def raw(old, new):
        result = syscall(
            ctypes.c_long(number),
            ctypes.c_int(AT_FDCWD),
            ctypes.c_char_p(old),
            ctypes.c_int(AT_FDCWD),
            ctypes.c_char_p(new),
            ctypes.c_uint(RENAME_NOREPLACE),
        )
        return result, ctypes.get_errno() if result else 0

    return raw


def native():
    global _native
    if _native is None:
        _native = _renameat2() or (lambda _old, _new: (-1, errno.ENOSYS))
    return _native


def rename_noreplace(source, target):
    """Move `source` to `target`, never replacing one. FileExistsError if `target` exists.

    Returns "renameat2" or "link" (the documented fallback) for diagnostics.
    """
    old, new = os.fsencode(source), os.fsencode(target)
    result, code = native()(old, new)
    if result == 0:
        return "renameat2"
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), str(target))
    if code not in UNSUPPORTED:
        raise OSError(code, os.strerror(code), str(target))
    os.link(source, target, follow_symlinks=False)
    os.unlink(source)
    return "link"


def same_file(a, b):
    """True when both names are links to one inode (never following a symlink)."""
    try:
        first, second = os.lstat(a), os.lstat(b)
    except FileNotFoundError:
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)
