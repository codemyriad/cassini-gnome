# SPDX-License-Identifier: GPL-3.0-or-later
"""D39 stable unpacked companion, using only POSIX/coreutils on a Flatpak host."""

import base64
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
from functools import lru_cache
from pathlib import Path

from .companion import EXTENSION_ID, in_flatpak

LIMIT = 8 * 1024 * 1024
MARKER = "io.codemyriad.cassini.browser-companion-v1"
# The same bounded transaction runs natively and through the existing host
# bridge. No host Python, profile/policy editing or new Flatpak grant is used.
TRANSACTION = r"""set -eu
umask 077
operation=$1
destination=$2
expected=$3
parent=${destination%/*}
previous=$parent/.browser-companion.previous
lock=$parent/.browser-companion.lock
[ ! -L "$destination" ] && [ ! -L "$previous" ] && [ ! -L "$lock" ] || exit 78
mkdir -p -- "$parent"
locked=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir -- "$lock" 2>/dev/null; then locked=true; break; fi
    [ ! -L "$lock" ] || exit 78
    # A directory may be released/reacquired between any two probes. Treat a
    # missing/non-directory lock as contention, without claiming ownership.
    if [ -f "$lock/pid" ] && [ ! -L "$lock/pid" ]; then
        owner=$(head -c 24 -- "$lock/pid" 2>/dev/null || :)
        case "$owner" in
            '') : ;; # The owner may still be writing, or may just have left.
            *[!0-9]*) exit 78 ;;
            *) if ! kill -0 "$owner" 2>/dev/null &&
                  [ "$(head -c 24 -- "$lock/pid" 2>/dev/null || :)" = "$owner" ]; then
                   rm -f -- "$lock/pid"
                   rmdir -- "$lock" 2>/dev/null || :
               fi ;;
        esac
    fi
    sleep 0.05
done
[ "$locked" = true ] || exit 75
printf '%s' "$$" > "$lock/pid"
stage=''
cleanup() {
    [ -z "$stage" ] || rm -rf -- "$stage"
    rm -f -- "$lock/pid"
    rmdir -- "$lock" 2>/dev/null || :
}
trap cleanup 0
trap 'exit 1' HUP INT TERM
managed() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ -f "$1/.cassini-copy" ] && [ ! -L "$1/.cassini-copy" ] &&
        [ "$(head -c 128 -- "$1/.cassini-copy")" = io.codemyriad.cassini.browser-companion-v1 ]
}
# Recover a crash between the two portable renames. A successful new publish
# never deletes the only copy; recovery is confined to our marked sibling.
if [ ! -e "$destination" ] && [ -e "$previous" ]; then
    managed "$previous" || exit 78
    mv -T -- "$previous" "$destination"
fi
if [ -e "$destination" ]; then
    [ -d "$destination" ] && [ -f "$destination/manifest.json" ] && [ ! -L "$destination/manifest.json" ] || exit 78
fi
if [ "$operation" = inspect ]; then
    [ ! -e "$destination" ] || head -c 65537 -- "$destination/manifest.json"
    exit 0
fi
[ "$operation" = publish ] || exit 78
# Compare-and-swap under the directory lock: a second app must never replace
# a newer copy installed since the caller inspected the manifest.
if [ -e "$destination" ]; then
    managed "$destination" || exit 78
    actual=$(sha256sum -- "$destination/manifest.json")
    actual=${actual%% *}
    [ "$actual" = "$expected" ] || exit 75
else
    [ "$expected" = missing ] || exit 75
fi
stage=$(mktemp -d "$parent/.browser-companion-XXXXXXXX")
while IFS= read -r name; do
    [ "$name" != DONE ] || break
    case "$name" in ''|/*|..|../*|*/../*|*/..|.|./*|*/./*|*/.|*\\*) exit 78 ;; esac
    IFS= read -r encoded || exit 78
    case "$name" in */*) mkdir -p -- "$stage/${name%/*}" ;; esac
    printf '%s' "$encoded" | base64 -d > "$stage/$name"
    chmod 600 "$stage/$name"
done
[ "$name" = DONE ] && [ -f "$stage/manifest.json" ] || exit 78
printf '%s' io.codemyriad.cassini.browser-companion-v1 > "$stage/.cassini-copy"
if [ -e "$destination" ]; then
    if [ -e "$previous" ]; then managed "$previous" || exit 78; rm -rf -- "$previous"; fi
    mv -T -- "$destination" "$previous"
    if ! mv -T -- "$stage" "$destination"; then
        mv -T -- "$previous" "$destination"
        exit 1
    fi
    stage=''
    rm -rf -- "$previous"
else
    mv -T -- "$stage" "$destination"
    stage=''
fi
"""


def bundle_directory():
    here = Path(__file__).resolve().parent
    for root in (here.parent, here.parents[1]):
        candidate = root / "browser-extension"
        if (candidate / "manifest.json").is_file():
            return candidate
    raise OSError("Bundled browser companion is unavailable")


@lru_cache(maxsize=1)
def store_listing_public(config=None):
    try:
        return (
            json.loads(Path(config or Path(__file__).with_name("paths.json")).read_text()).get("storeListingPublic")
            is True
        )
    except (OSError, ValueError):
        return False


def version(manifest):
    value = manifest.get("version")
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        raise ValueError("Invalid companion version")
    parts = tuple(int(part) for part in value.split("."))
    if any(part > 65535 for part in parts):
        raise ValueError("Invalid companion version")
    return parts + (0,) * (4 - len(parts))


def identity(manifest):
    try:
        raw = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
        result = "".join(chr(ord("a") + int(c, 16)) for c in raw)
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Companion key is unavailable") from error
    if result != EXTENSION_ID:
        raise ValueError("Companion identity differs from the pinned public key")


def bundle_files(directory):
    files, size = {}, 0
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory)
        if any(part in ("tests", "__pycache__", ".git") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Companion resources must not be symbolic links")
        if path.is_dir():
            continue
        name = relative.as_posix()
        if (
            name == "DONE"
            or path.suffix.lower() in (".pem", ".key", ".p12", ".pfx", ".pkcs8", ".pkcs12", ".keystore")
            or any(c in name for c in "\r\n\\\0")
        ):
            raise ValueError("Unexpected companion resource")
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Companion resources must be regular files")
        size += path.stat().st_size
        if size > LIMIT or len(files) >= 128:
            raise ValueError("Companion resources exceed the size limit")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Companion resources must be regular files")
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT or sum(map(len, files.values())) + len(data) > LIMIT:
            raise ValueError("Companion resources exceed the size limit")
        files[name] = data
    manifest = json.loads(files["manifest.json"])
    identity(manifest)
    version(manifest)
    return files, manifest


def _run(operation, destination, expected="missing", payload=None):
    command = ["/bin/sh", "-c", TRANSACTION, "cassini-companion-copy", operation, str(destination), expected]
    if in_flatpak():
        from .flatpak_bridge import deployment, host_command

        deployment()
        command = host_command(command, watch=True)
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise OSError(errno.ETIMEDOUT, "Browser companion preparation timed out") from error
    if result.returncode:
        error = OSError(
            errno.EAGAIN if result.returncode == 75 else errno.EIO, "Could not prepare the browser companion"
        )
        error.returncode = result.returncode
        error.add_note(f"{operation} exit {result.returncode}: {result.stderr[:512].decode(errors='replace')}")
        raise error
    if len(result.stdout) > 65536:
        raise ValueError("Installed companion manifest exceeds limit")
    return result.stdout


def prepare(directory=None, *, destination=None):
    files, bundled = bundle_files(directory or bundle_directory())
    if destination is None:
        if in_flatpak():
            from .flatpak_bridge import deployment, host_info

            deployment()
            data = host_info()[1]
        else:
            data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        destination = data / "cassini/browser-companion"
    destination = Path(destination)
    if not destination.is_absolute():
        raise ValueError("Companion destination must be absolute")
    payload = (
        b"".join(name.encode() + b"\n" + base64.b64encode(data) + b"\n" for name, data in files.items()) + b"DONE\n"
    )
    for _attempt in range(3):
        previous = _run("inspect", destination)
        if previous:
            installed = json.loads(previous)
            identity(installed)
            if version(installed) >= version(bundled):
                return destination
        expected = hashlib.sha256(previous).hexdigest() if previous else "missing"
        try:
            _run("publish", destination, expected, payload)
            return destination
        except OSError as error:
            if getattr(error, "returncode", None) != 75:
                raise
    raise OSError("Companion changed during preparation")
