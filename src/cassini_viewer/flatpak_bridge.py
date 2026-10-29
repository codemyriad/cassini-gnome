# SPDX-License-Identifier: GPL-3.0-or-later
"""Host browser registration through the explicitly granted Flatpak service."""

import configparser
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

APPLICATIONS = {"com.gocassini.Viewer", "com.gocassini.Viewer.Devel"}
COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
    "brave",
)
PROBE = """set -eu
printf '%s\\0' "${XDG_CONFIG_HOME:-$HOME/.config}" "${XDG_DATA_HOME:-$HOME/.local/share}"
for command in flatpak "$@"; do
    executable=$(command -v "$command" || :)
    printf '%s\\0' "$executable"
done
"""
WRITE = """set -eu
umask 077
destination=$1
directory=${destination%/*}
mkdir -p -- "$directory"
temporary=$(mktemp "$directory/.cassini-XXXXXXXX")
trap 'rm -f -- "$temporary"' 0
trap 'exit 1' HUP INT TERM
cat > "$temporary"
chmod "$2" "$temporary"
mv -fT -- "$temporary" "$destination"
"""


@dataclass(frozen=True)
class Deployment:
    application: str
    arch: str
    branch: str


def deployment(info_path="/.flatpak-info"):
    info = configparser.ConfigParser(interpolation=None)
    try:
        if not info.read(info_path):
            raise ValueError("No Flatpak deployment information")
        application = info["Application"]["name"]
        arch, branch = info["Instance"]["arch"], info["Instance"]["branch"]
        permission = info.get("Session Bus Policy", "org.freedesktop.Flatpak", fallback="")
        if application not in APPLICATIONS or permission not in {"talk", "own"}:
            raise ValueError("The Flatpak host bridge is not granted")
        if any(not value or not all(c.isalnum() or c in "._-" for c in value) for value in (arch, branch)):
            raise ValueError("Invalid Flatpak deployment")
        return Deployment(application, arch, branch)
    except (KeyError, ValueError, configparser.Error) as error:
        raise OSError("The Flatpak host bridge is unavailable") from error


def host_command(command, *, watch=False):
    spawn = shutil.which("flatpak-spawn")
    if not spawn:
        raise OSError("Flatpak host launch is unavailable")
    return [spawn, "--host", "--directory=/", *(["--watch-bus"] if watch else []), *command]


def detached_command(command):
    return host_command(["/bin/sh", "-c", '"$@" </dev/null >/dev/null 2>&1 &', "cassini-host-launch", *command])


def host_info():
    try:
        result = subprocess.run(
            host_command(["/bin/sh", "-c", PROBE, "cassini-probe", *COMMANDS], watch=True),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=2,
        )
        values = result.stdout.decode().split("\0")
        if len(values) != len(COMMANDS) + 4 or values[-1] != "":
            raise ValueError("Invalid host browser inventory")
        config, data, flatpak, *executables = values[:-1]
        if any(not Path(p).is_absolute() for p in (config, data, flatpak)):
            raise ValueError("Host paths must be absolute")
        if any(p and not Path(p).is_absolute() for p in executables):
            raise ValueError("Browser paths must be absolute")
        return Path(config), Path(data), flatpak, dict(zip(COMMANDS, executables, strict=True))
    except (subprocess.SubprocessError, UnicodeError, ValueError) as error:
        raise OSError("Could not reach the host browser") from error


def probe():
    """Return (ready, retryable); sandbox permissions cannot change in place."""
    try:
        deployment()
    except OSError:
        return False, False
    try:
        host_info()
        return True, False
    except OSError:
        return False, True


def available():
    return probe()[0]


def installed_browsers():
    from .companion import installed_browsers as detect

    deployment()
    config, _, _, executables = host_info()
    return detect(config=config, which=executables.get, host=False)


def broker_command(socket_path):
    """Detach the broker from the UI/native-host sandbox's process lifetime.

    Bubblewrap waits for descendants, even after their original command exits.
    The short host shell must return, so no flatpak-spawn descendant keeps the
    caller alive. The dedicated broker exits after its last client is idle.
    """
    target = deployment()
    _, _, flatpak, _ = host_info()
    return detached_command(
        [
            flatpak,
            "run",
            "--unshare=network",
            "--arch=" + target.arch,
            "--branch=" + target.branch,
            "--command=python3",
            "--env=PYTHONPATH=/app/share/cassini-viewer",
            target.application,
            "-m",
            "cassini_viewer.browser_service",
            "--serve",
            str(socket_path),
            "--idle-timeout",
            "10",
        ]
    )


def _write(path, content, mode):
    try:
        subprocess.run(
            host_command(["/bin/sh", "-c", WRITE, "cassini-register", str(path), mode], watch=True),
            input=content.encode(),
            capture_output=True,
            check=True,
            timeout=5,
        )
    except subprocess.SubprocessError as error:
        raise OSError("Could not register the browser companion") from error


def register(browsers=None):
    from .companion import ORIGIN
    from .companion import installed_browsers as detect

    target = deployment()
    config, data, flatpak, executables = host_info()
    browsers = detect(config=config, which=executables.get, host=False) if browsers is None else browsers
    if not browsers:
        raise OSError("No supported browser is installed")
    directory = data / "cassini/native-hosts" / target.application
    template = Path(__file__).with_name("flatpak-native-host.sh").read_text()
    hosts = {}
    for name, command in (
        ("com.codemyriad.cassini", "cassini-browser-host"),
        ("com.codemyriad.cassini.control", "cassini-browser-control-host"),
    ):
        content = template
        for key, value in dict(
            FLATPAK=flatpak, APPLICATION=target.application, ARCH=target.arch, BRANCH=target.branch, COMMAND=command
        ).items():
            content = content.replace("@" + key + "@", shlex.quote(value))
        executable = directory / command
        _write(executable, content, "700")
        hosts[name] = executable
    written = []
    for browser in browsers:
        for name, executable in hosts.items():
            destination = browser.config / "NativeMessagingHosts" / (name + ".json")
            manifest = dict(
                name=name,
                description="Cassini local meeting recorder",
                path=str(executable),
                type="stdio",
                allowed_origins=[ORIGIN],
            )
            _write(destination, json.dumps(manifest, indent=2) + "\n", "600")
            written.append(destination)
    return written
