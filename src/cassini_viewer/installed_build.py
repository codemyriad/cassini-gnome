# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 immutable startup identity and verified installed-build discovery.

All discovery runs on a worker. Flatpak's /app is the running deployment, so
query its host installation through the existing bridge, never that old mount.
"""

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from uuid import uuid4

LIMIT = 128 * 1024


def lifecycle_log(message, *args):
    logger = logging.getLogger("cassini.lifecycle")
    logger.setLevel(logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(handler)
    logger.info(message, *args)


def decode(raw):
    if len(raw) > LIMIT:
        raise ValueError("Application identity exceeds limit")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or (type(value.get("schema")) is not int or value["schema"] != 1)
        or not isinstance(value.get("version"), str)
        or not isinstance(value.get("files"), dict)
        or not 1 <= len(value["files"]) <= 512
    ):
        raise ValueError("Invalid application identity")
    for digest in [value.get("buildId"), value.get("launcher"), *value["files"].values()]:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid application identity hash")
    for name in value["files"]:
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or path.as_posix() != name
            or ".." in path.parts
            or any(c in name for c in "\n\r\\\0")
        ):
            raise ValueError("Invalid application identity path")
    fields = {key: val for key, val in value.items() if key != "buildId"}
    actual = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual != value["buildId"]:
        raise ValueError("Application identity is damaged")
    return value


def _open_regular(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("Application identity contains a non-regular file")
    return os.fdopen(descriptor, "rb")


def read(path):
    with _open_regular(path) as stream:
        return decode(stream.read(LIMIT + 1))


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as stream:
        while data := stream.read(65536):
            size += len(data)
            if size > 16 * 1024 * 1024:
                raise OSError("Application identity file exceeds limit")
            digest.update(data)
    return digest.hexdigest()


def verified(value, root, launcher):
    """An unpack in progress is not a completed upgrade, even at the same version."""
    try:
        return file_digest(launcher) == value["launcher"] and all(
            file_digest(Path(root) / name) == digest for name, digest in value["files"].items()
        )
    except OSError:
        return False


class InstalledBuild:
    def __init__(self, root, launcher, version, *, flatpak=False):
        self.root, self.launcher = Path(root), Path(launcher)
        self.running = read(self.root / "build-info.json")
        self.running_version = version
        self.flatpak = flatpak
        self.restart = None

    def announce(self):
        lifecycle_log(
            "Cassini %s build=%s start=%s pid=%s",
            self.running_version,
            self.running["buildId"],
            uuid4().hex,
            os.getpid(),
        )

    def changed(self):
        if self.flatpak:
            return self._host_changed()
        value = read(self.root / "build-info.json")
        if (value["version"], value["buildId"]) == (self.running_version, self.running["buildId"]):
            return False
        # The manifest can be unpacked before the remaining package files.
        if not verified(value, self.root, self.launcher):
            raise OSError("Application update is still being installed")
        return read(self.root / "build-info.json") == value

    def _host_changed(self):
        from .flatpak_bridge import deployment, host_command, host_info

        target = deployment()
        _, _, flatpak, _ = host_info()
        command = [flatpak, "info", "--show-location", "--arch=" + target.arch, target.application, target.branch]
        result = subprocess.run(host_command(command, watch=True), capture_output=True, check=True, timeout=5)
        location = result.stdout.decode().strip()
        if len(result.stdout) > 4096 or not location.startswith("/") or any(c in location for c in "\r\n\\\0"):
            raise OSError("Invalid installed application location")
        root = Path(location) / "files/share/cassini-viewer"
        identity = root / "build-info.json"
        result = subprocess.run(
            host_command(["head", "-c", str(LIMIT + 1), "--", str(identity)], watch=True),
            capture_output=True,
            check=True,
            timeout=5,
        )
        value = decode(result.stdout)
        if (value["version"], value["buildId"]) == (self.running_version, self.running["buildId"]):
            return False
        pairs = [(str(root / name), digest) for name, digest in value["files"].items()]
        pairs.append((str(Path(location) / "files/bin/cassini-viewer"), value["launcher"]))
        checks = "".join(f"{digest}  {name}\n" for name, digest in pairs).encode()
        subprocess.run(
            host_command(["sha256sum", "-c"], watch=True),
            input=checks,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=5,
        )
        return True

    def reexec(self, *, show, automatic, files=(), writing=False):
        arguments = list(files)
        if not show:
            arguments.append("--cassini-resume-background")
            if writing:
                arguments.append("--cassini-resume-writing")
            if automatic:
                arguments.append("--cassini-resume-automatic")
        if self.flatpak:
            from .flatpak_bridge import deployment, detached_command, host_info

            target = deployment()
            _, _, flatpak, _ = host_info()
            command = detached_command(
                [
                    flatpak,
                    "run",
                    "--arch=" + target.arch,
                    "--branch=" + target.branch,
                    "--command=cassini-viewer",
                    target.application,
                    *arguments,
                ]
            )
            # Replace the old GTK process first, dropping its GApplication bus
            # owner before the host starts the new deployment. No --watch-bus.
            os.execv(command[0], command)
        # Honour the newly installed interpreter and launcher flags too.
        os.execv(str(self.launcher), [str(self.launcher), *arguments])
