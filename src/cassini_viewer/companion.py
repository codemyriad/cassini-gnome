# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-user companion registration; no root, shell commands or manual IDs."""

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

EXTENSION_ID = "pcafdaahfhjiolfpfnlhmljfgoabbaij"
ADD_URL = "https://chromewebstore.google.com/detail/" + EXTENSION_ID
ORIGIN = "chrome-extension://" + EXTENSION_ID + "/"
MANIFEST_LIMIT = 65536
LEGACY_HOSTS = {
    "com.codemyriad.cassini": {"cassini-browser-host", "native_host.py", "com.codemyriad.cassini.py"},
    "com.codemyriad.cassini.control": {
        "cassini-browser-control-host",
        "control_host.py",
        "com.codemyriad.cassini.control.py",
    },
}
CONFIG_DIRS = ("google-chrome", "chromium", "microsoft-edge", "BraveSoftware/Brave-Browser")


class SetupError(OSError):
    def __init__(self, reason, *, retryable=False, available=True):
        super().__init__("No supported browser is installed" if reason == "noBrowser" else "Browser setup: " + reason)
        self.reason, self.retryable, self.available = reason, retryable, available


def legacy_owned(filename, content):
    """D27 ownership is name + JSON name + executable basename, never origin."""
    name = filename.removesuffix(".json")
    if filename != name + ".json" or name not in LEGACY_HOSTS or len(content) > MANIFEST_LIMIT:
        return False

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Ambiguous native host manifest")
            value[key] = item
        return value

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
        return (
            isinstance(value, dict)
            and value.get("name") == name
            and isinstance(value.get("path"), str)
            and Path(value["path"]).name in LEGACY_HOSTS[name]
        )
    except (ValueError, TypeError, RecursionError):
        return False


def manifest_directories(*, config=None, browsers=None):
    if browsers is not None:
        return [b.config / "NativeMessagingHosts" for b in browsers]
    config = Path(config or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [config / directory / "NativeMessagingHosts" for directory in CONFIG_DIRS]


def cleanup_legacy_directory(directory):
    """Read bounded regular files without following symlinks; preserve changes."""
    removed = []
    for name in LEGACY_HOSTS:
        path = Path(directory) / (name + ".json")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    continue
                if not legacy_owned(path.name, stream.read(MANIFEST_LIMIT + 1)):
                    continue
                now = path.lstat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    now.st_dev,
                    now.st_ino,
                    now.st_size,
                    now.st_mtime_ns,
                ):
                    continue
                path.unlink()
                removed.append(path)
        except OSError:
            continue  # Missing, inaccessible and non-owned files stay untouched.
    return removed


def cleanup_legacy(*, browsers=None):
    if in_flatpak():
        from .flatpak_bridge import cleanup_legacy as host_cleanup

        return host_cleanup(browsers)
    return [
        path for directory in manifest_directories(browsers=browsers) for path in cleanup_legacy_directory(directory)
    ]


def start_legacy_cleanup():
    def cleanup():
        try:
            cleanup_legacy()
        except OSError:
            logging.getLogger(__name__).warning("Could not clean legacy native hosts", exc_info=True)

    # Host-side IO neither blocks GTK nor keeps Quit waiting.
    thread = threading.Thread(target=cleanup, name="cassini-host-cleanup", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class Browser:
    name: str
    executable: str
    config: Path


def in_flatpak():
    return bool(os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").is_file())


def probe():
    """Return readiness and whether a failed host check may recover in place."""
    if not in_flatpak():
        return True, False
    from .flatpak_bridge import probe as host_probe

    return host_probe()


def available():
    return probe()[0]


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
    return open_url(browser, ADD_URL, launch=launch)


def open_extensions(browser, *, launch=subprocess.Popen):
    url = "edge://extensions/" if browser.name == "Edge" else "chrome://extensions/"
    return open_url(browser, "--app=" + url, launch=launch)


def open_url(browser, url, *, launch=subprocess.Popen):
    command = [browser.executable, url]
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
        ("io.codemyriad.cassini", "native_host.py"),
        ("io.codemyriad.cassini.control", "control_host.py"),
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


def profile_inventory():
    """Worker-only fingerprint: executable/config roots and profile names, no content."""
    browsers = installed_browsers()
    if in_flatpak():
        from .flatpak_bridge import profile_inventory as host_profiles

        profiles = host_profiles(browsers)
    else:
        profiles = []
        for browser in browsers:
            try:
                children = sorted(
                    p.name
                    for p in browser.config.iterdir()
                    if (p.name == "Default" or p.name.startswith("Profile ")) and p.is_dir()
                )
                profiles.append((str(browser.config), True, children))
            except FileNotFoundError:
                profiles.append((str(browser.config), False, []))
            except OSError as error:
                profiles.append((str(browser.config), "unavailable-" + str(error.errno), []))
    values = [(b.name, b.executable, str(b.config)) for b in browsers]
    return hashlib.sha256(json.dumps([values, profiles], sort_keys=True).encode()).hexdigest()


def _register_browser(browser, hosts):
    written = []
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
        destination = directory / (name + ".json")
        try:
            with os.fdopen(os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SetupError("unsafe")
                previous = stream.read(MANIFEST_LIMIT + 1)
            if len(previous) <= MANIFEST_LIMIT and json.loads(previous) == data:
                written.append(destination)
                continue
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeError):
            pass  # A damaged own manifest is replaced atomically.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
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


def register(*, browsers=None, hosts=None):
    """Atomically write both exact-origin manifests, only under user configs."""
    if in_flatpak():
        from .flatpak_bridge import register as host_register

        return host_register(browsers)
    browsers = installed_browsers() if browsers is None else browsers
    hosts = host_paths() if hosts is None else hosts
    if not browsers:
        raise SetupError("noBrowser")
    written, errors = [], []
    for browser in browsers:
        try:
            written.extend(_register_browser(browser, hosts))
        except OSError as error:
            errors.append(error)
    cleanup_legacy(browsers=browsers)
    if errors:
        raise errors[0]
    return written
