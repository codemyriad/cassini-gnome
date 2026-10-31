# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 native-host health and conservative development-build ownership."""

import contextlib
import fcntl
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .companion import MANIFEST_LIMIT, SetupError

LOG = logging.getLogger(__name__)
# Recognise old relays for D42 health checks after the Flatpak ID changes.
# Only flatpak_bridge.APPLICATIONS may be used for a new deployment.
APPLICATIONS = {
    "io.codemyriad.CassiniTalk",
    "io.codemyriad.CassiniTalk.Devel",
    "com.gocassini.Viewer",
    "com.gocassini.Viewer.Devel",
}


def protected_build():
    from .companion import in_flatpak

    if in_flatpak():
        return os.environ.get("FLATPAK_ID") != "io.codemyriad.CassiniTalk"
    try:
        value = json.loads(Path(__file__).with_name("paths.json").read_text())
    except (OSError, ValueError):
        return True
    return value.get("profile") != "default" or value.get("bindir") not in ("/usr/bin", "/usr/local/bin")


def native_read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SetupError("unsafe")
        data = stream.read(MANIFEST_LIMIT + 1)
    if len(data) > MANIFEST_LIMIT:
        raise SetupError("unsafe")
    return data


def manifest(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and isinstance(value.get("path"), str) and Path(value["path"]).is_absolute():
            return value
    except (ValueError, UnicodeError):
        pass
    return None


def relay_target(path, content):
    """Recognise our existing relay; do not run or source its shell content."""
    path = Path(path)
    if path.parent.name not in APPLICATIONS or path.parent.parent.name != "native-hosts":
        return None
    try:
        script = content.decode()
        start = 0 if script.startswith("exec ") else script.index("\nexec ") + 1
        # shlex preserves quoted literal newlines in host paths. A shell line
        # continuation becomes a standalone newline token; our relay has no
        # newline-only arguments, so discard just those tokens.
        words = [word for word in shlex.split(script[start:]) if word != "\n"]
        if words[0] != "exec" or Path(words[1]).name != "flatpak" or words[2] != "run":
            raise ValueError("Invalid Cassini relay command")
        app = words[-1]
        if app != path.parent.name:
            raise ValueError("Invalid Cassini relay application")
        options = dict(word[2:].split("=", 1) for word in words if word.startswith("--") and "=" in word)
        command, arch, branch = (options[key] for key in ("command", "arch", "branch"))
        if command not in ("cassini-browser-host", "cassini-browser-control-host"):
            raise ValueError("Invalid Cassini relay host")
        if any(not v or not all(c.isalnum() or c in "._-" for c in v) for v in (arch, branch)):
            raise ValueError("Invalid Cassini relay deployment")
        return app, arch, branch, command
    except (ValueError, UnicodeError, KeyError, IndexError, StopIteration):
        return False


def installed_relay(target, *, run=subprocess.run, flatpak=None):
    app, arch, branch, command = target
    executable = flatpak or shutil.which("flatpak")
    if not executable:
        return False
    try:
        # A successful inventory distinguishes 'absent' from a broken helper.
        inventory = run(
            [executable, "list", "--app", "--columns=application,arch,branch"],
            capture_output=True,
            check=True,
            timeout=3,
        )
        if len(inventory.stdout) > 1024 * 1024:
            raise OSError("Flatpak inventory exceeds limit")
        rows = [line.split() for line in inventory.stdout.decode().splitlines()]
        if [app, arch, branch] not in rows:
            return False
        result = run(
            [executable, "info", "--show-location", "--arch=" + arch, app, branch],
            capture_output=True,
            check=True,
            timeout=3,
        )
        location = result.stdout.decode().strip()
        if len(result.stdout) > 4096 or not Path(location).is_absolute():
            raise OSError("Invalid Flatpak installation location")
        probe = run(
            [
                "/bin/sh",
                "-c",
                '[ -f "$1" ] && [ -x "$1" ]',
                "cassini-host-target",
                str(Path(location) / "files/bin" / command),
            ],
            capture_output=True,
            timeout=2,
        )
        return probe.returncode == 0
    except (subprocess.SubprocessError, UnicodeError) as error:
        raise OSError("Could not inspect the registered Flatpak") from error


def native_target(path):
    path = Path(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    if path.parent.name not in APPLICATIONS or path.parent.parent.name != "native-hosts":
        return True
    content = native_read(path)
    target = relay_target(path, content or b"")
    if target is False:
        raise OSError("Could not identify the registered Flatpak relay")
    return bool(target and installed_relay(target))


def decision(previous, desired, *, force, protected, target_ready):
    value = manifest(previous)
    if value is None or force:
        return True, False
    if value["path"] == desired["path"]:
        # Still inspect the target: an idempotent manifest can point at an
        # executable relay whose Flatpak deployment has since been removed.
        return True, not target_ready(value["path"])
    ready = target_ready(value["path"])
    # Unknown foreign ownership is protected too; never guess that a valid
    # release host is disposable merely because this build cannot identify it.
    return force or not protected or not ready, not ready


@contextlib.contextmanager
def directory_lock(directory):
    path = Path(directory) / ".cassini-registration.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SetupError("unsafe")
        deadline = time.monotonic() + 0.5
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SetupError("temporary", retryable=True) from None
                time.sleep(0.02)
        yield
    finally:
        os.close(descriptor)


def health(browsers):
    from .companion import in_flatpak

    if in_flatpak():
        from .flatpak_bridge import registration_health

        return registration_health(browsers)
    ready = {}
    for browser in browsers:
        for name in ("io.codemyriad.cassini", "io.codemyriad.cassini.control"):
            value = manifest(native_read(browser.config / "NativeMessagingHosts" / (name + ".json")))
            if value is None:
                return True
            path = value["path"]
            if path not in ready:
                ready[path] = native_target(path)
            if not ready[path]:
                return True
    return False


def dead_registration(browsers):
    from .companion import in_flatpak

    if in_flatpak():
        from .flatpak_bridge import _registered_host_ready, _registration_files

        paths = [
            browser.config / "NativeMessagingHosts" / (name + ".json")
            for browser in browsers
            for name in ("io.codemyriad.cassini", "io.codemyriad.cassini.control")
        ]
        return any(
            (value := manifest(raw)) is not None and not _registered_host_ready(value["path"])
            for raw in _registration_files(paths).values()
        )
    return any(
        (value := manifest(native_read(browser.config / "NativeMessagingHosts" / (name + ".json")))) is not None
        and not native_target(value["path"])
        for browser in browsers
        for name in ("io.codemyriad.cassini", "io.codemyriad.cassini.control")
    )
