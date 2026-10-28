# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-user companion registration; no root, shell commands or manual IDs."""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXTENSION_ID = "pcafdaahfhjiolfpfnlhmljfgoabbaij"
ADD_URL = "https://chromewebstore.google.com/detail/" + EXTENSION_ID
ORIGIN = "chrome-extension://" + EXTENSION_ID + "/"


@dataclass(frozen=True)
class Browser:
    name: str
    executable: str
    config: Path


def in_flatpak():
    return bool(os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").is_file())


def available():
    if not in_flatpak():
        return True
    from .flatpak_bridge import available as host_available

    return host_available()


def installed_browsers(*, config=None, which=shutil.which, host=True):
    if host and in_flatpak():
        from .flatpak_bridge import installed_browsers as host_browsers

        return host_browsers()
    config = Path(config or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    result = []
    for name, commands, directory in (
        ("Chrome", ("google-chrome", "google-chrome-stable"), "google-chrome"),
        ("Chromium", ("chromium", "chromium-browser"), "chromium"),
        ("Edge", ("microsoft-edge", "microsoft-edge-stable"), "microsoft-edge"),
        ("Brave", ("brave-browser", "brave"), "BraveSoftware/Brave-Browser"),
    ):
        executable = next((path for command in commands if (path := which(command))), None)
        if executable:
            result.append(Browser(name, executable, config / directory))
    return result


def open_add(browser, *, launch=subprocess.Popen):
    """The sole Add entry point; a click never means the extension is installed."""
    command = [browser.executable, ADD_URL]
    if in_flatpak():
        from .flatpak_bridge import detached_command

        command = detached_command(command)
    return launch(command, stdin=subprocess.DEVNULL, start_new_session=True)


def host_paths():
    here = Path(__file__).resolve().parent
    paths = here / "paths.json"
    bindir = json.loads(paths.read_text()).get("bindir") if paths.is_file() else None
    result = {}
    for name, development in (
        ("com.codemyriad.cassini", "native_host.py"),
        ("com.codemyriad.cassini.control", "control_host.py"),
    ):
        command = "cassini-browser-control-host" if name.endswith(".control") else "cassini-browser-host"
        if bindir:
            candidate = Path(bindir) / command
        else:
            local = here.parents[1] / "browser-extension" / development
            executable = shutil.which(command)
            candidate = local if local.is_file() else Path(executable) if executable else local
        candidate = candidate.resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise OSError("Native messaging host is not executable")
        result[name] = candidate
    return result


def register(*, browsers=None, hosts=None):
    """Atomically write both exact-origin manifests, only under user configs."""
    if in_flatpak():
        from .flatpak_bridge import register as host_register

        return host_register(browsers)
    browsers = installed_browsers() if browsers is None else browsers
    hosts = host_paths() if hosts is None else hosts
    if not browsers:
        raise OSError("No supported browser is installed")
    written = []
    for browser in browsers:
        directory = browser.config / "NativeMessagingHosts"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, executable in hosts.items():
            executable = Path(executable).resolve(strict=True)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise OSError("Native messaging host is not executable")
            data = dict(
                name=name,
                description="Cassini local meeting recorder",
                path=str(executable),
                type="stdio",
                allowed_origins=[ORIGIN],
            )
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(data, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                destination = directory / (name + ".json")
                temporary.replace(destination)
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                written.append(destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    return written
