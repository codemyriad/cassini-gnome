# SPDX-License-Identifier: GPL-3.0-or-later
"""Host browser registration through the explicitly granted Flatpak service."""

import configparser
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

APPLICATIONS = {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}
COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
    "brave",
    "/snap/bin/chromium",
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
# A sentinel retains trailing newlines in command substitutions. cmp belongs
# to diffutils, which is not guaranteed on a host with only coreutils.
content=$(cat; printf '.')
content=${content%.}
# Compare-and-swap under the same flock inode as native registration. Never
# replace a valid release manifest installed since the sandbox inspected it.
if [ -n "${3:-}" ]; then
 ancestor=$directory
 while [ ! -e "$ancestor" ] && [ "$ancestor" != / ]; do ancestor=${ancestor%/*}; done
 [ -w "$ancestor" ] || exit 77
 mkdir -p -- "$directory"
 lock=$directory/.cassini-registration.lock
 [ ! -L "$lock" ] && { [ ! -e "$lock" ] || [ -f "$lock" ]; } || exit 78
 exec 9>>"$lock"
 locked=false
 for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if flock -n 9; then locked=true; break; fi
  sleep .05
 done
 [ "$locked" = true ] || exit 75
 [ ! -L "$destination" ] || exit 78
 if [ -e "$destination" ]; then
  [ -f "$destination" ] || exit 78
  actual=$(sha256sum < "$destination"); actual=${actual%% *}
 else actual=missing; fi
 [ "$actual" = "$3" ] || exit 75
fi
[ ! -L "$destination" ] || exit 78
if [ -e "$destination" ] && [ ! -f "$destination" ]; then exit 78; fi
if [ -f "$destination" ]; then
    previous=$(head -c 65537 -- "$destination"; printf '.')
else previous='.'; fi
if [ -f "$destination" ] && [ "${previous%.}" = "$content" ]; then
    if [ "$2" = 700 ] && [ ! -x "$destination" ]; then
        [ -w "$destination" ] || exit 77
        chmod 700 "$destination"
    fi
    exit 0
fi
ancestor=$directory
while [ ! -e "$ancestor" ] && [ "$ancestor" != / ]; do ancestor=${ancestor%/*}; done
[ -w "$ancestor" ] || exit 77
mkdir -p -- "$directory"
temporary=$(mktemp "$directory/.cassini-XXXXXXXX")
trap 'rm -f -- "$temporary"' 0
trap 'exit 1' HUP INT TERM
printf '%s' "$content" > "$temporary"
chmod "$2" "$temporary"
mv -fT -- "$temporary" "$destination"
"""
READ_LEGACY = """set -eu
if [ -f "$1" ] && [ ! -L "$1" ] && [ -r "$1" ]; then
    head -c 65537 -- "$1"
fi
"""
DELETE_LEGACY = """set -eu
destination=$1
[ -f "$destination" ] && [ ! -L "$destination" ] || exit 0
expected=$(base64)
current=$(head -c 65537 -- "$destination" | base64)
if [ ! -L "$destination" ] && [ "$expected" = "$current" ]; then
    rm -f -- "$destination"
    printf 'removed\\n'
fi
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


BROWSER_LAUNCH = r"""
"$@" </dev/null >/dev/null 2>&1 &
child=$!
report_exit() {
    wait "$child"
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'Browser launcher exited with status %s\n' "$status" >&2
    fi
    exit "$status"
}
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$child" 2>/dev/null || report_exit
    sleep .05
done
kill -0 "$child" 2>/dev/null || report_exit
# A live browser is intentionally detached, with no --watch-bus lifetime tie.
exit 0
"""


def detached_command(command, *, check_early_exit=False):
    script = BROWSER_LAUNCH if check_early_exit else '"$@" </dev/null >/dev/null 2>&1 &'
    return host_command(["/bin/sh", "-c", script, "cassini-host-launch", *command])


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
    from .companion import DESKTOP_NAMES
    from .companion import installed_browsers as detect

    deployment()
    config, data, flatpak, executables = host_info()
    # Read only known browser desktop entries, on the host worker. Never infer
    # a host home directory from XDG_CONFIG_HOME, which may be relocated.
    script = r"""set -eu
printf '%s\0' "$HOME"
for root in "${XDG_DATA_HOME:-$HOME/.local/share}/applications" "$HOME/.local/share/flatpak/exports/share/applications" /var/lib/flatpak/exports/share/applications /var/lib/snapd/desktop/applications /usr/local/share/applications /usr/share/applications; do
    for name in "$@"; do
        if [ -r "$root/$name" ]; then
            printf '%s\0' "$name"
            head -c 65536 -- "$root/$name"
            printf '\0'
        fi
    done
done
"""
    try:
        result = subprocess.run(
            host_command(["/bin/sh", "-c", script, "cassini-desktops", *DESKTOP_NAMES], watch=True),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=2,
        )
        values = result.stdout.decode().split("\0")
        home = Path(values.pop(0))
        if not home.is_absolute() or values[-1] != "" or len(values) % 2 != 1:
            raise ValueError("Invalid host desktop inventory")
        desktops = {}
        for name, content in zip(values[0:-1:2], values[1:-1:2], strict=True):
            if name not in DESKTOP_NAMES:
                raise ValueError("Unknown host desktop entry")
            desktops.setdefault(name, content)
    except (subprocess.SubprocessError, UnicodeError, ValueError, IndexError) as error:
        raise OSError("Could not inspect host browsers") from error
    return detect(
        config=config,
        which=lambda command: (
            flatpak
            if Path(command).name == "flatpak"
            else executables.get(command) or executables.get(Path(command).name)
        ),
        host=False,
        home=home,
        desktops=desktops,
    )


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


def profile_inventory(browsers):
    script = """for root in "$@"; do
printf '%s\\0' "$root"
if [ -d "$root" ]; then
    printf 'present\\0'
    for profile in "$root/Default" "$root"/Profile\\ *; do
        [ ! -d "$profile" ] || printf '%s\\0' "${profile##*/}"
    done
else printf 'missing\\0'; fi
done"""
    try:
        result = subprocess.run(
            host_command(["/bin/sh", "-c", script, "cassini-profiles", *[str(b.config) for b in browsers]], watch=True),
            capture_output=True,
            check=True,
            timeout=3,
        )
        if len(result.stdout) > 65536:
            raise OSError("Browser profile inventory exceeds limit")
        return result.stdout.decode("utf-8")
    except (subprocess.SubprocessError, UnicodeError) as error:
        raise OSError("Could not inspect host browser profiles") from error


def _write(path, content, mode, *, expected=None):
    from .companion import SetupError

    if len(content.encode()) > 65536:
        raise SetupError("unsafe")
    try:
        subprocess.run(
            host_command(["/bin/sh", "-c", WRITE, "cassini-register", str(path), mode, expected or ""], watch=True),
            input=content.encode(),
            capture_output=True,
            check=True,
            timeout=5,
        )
    except subprocess.SubprocessError as error:
        if getattr(error, "returncode", None) in (77, 78):
            raise SetupError("unwritable" if error.returncode == 77 else "unsafe") from error
        raise OSError("Could not register the browser companion") from error


def _cleanup_legacy_directories(directories):
    from .companion import LEGACY_HOSTS, legacy_owned

    removed = []
    for directory in directories:
        for name in LEGACY_HOSTS:
            path = directory / (name + ".json")
            try:
                content = subprocess.run(
                    host_command(["/bin/sh", "-c", READ_LEGACY, "cassini-cleanup", str(path)], watch=True),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=True,
                    timeout=2,
                ).stdout
                if not legacy_owned(path.name, content):
                    continue
                result = subprocess.run(
                    host_command(["/bin/sh", "-c", DELETE_LEGACY, "cassini-cleanup", str(path)], watch=True),
                    input=content,
                    capture_output=True,
                    check=True,
                    timeout=2,
                )
                if result.stdout == b"removed\n":
                    removed.append(path)
            except (OSError, subprocess.SubprocessError):
                return removed  # An unavailable host must not cause repeated timeouts.
    return removed


def cleanup_legacy(browsers=None):
    from .companion import manifest_directories

    deployment()
    config, _, _, _ = host_info()
    return _cleanup_legacy_directories(manifest_directories(config=config, browsers=browsers))


def register(browsers=None, *, force=False):
    from .companion import ORIGIN, SetupError
    from .registration import decision, protected_build

    target = deployment()
    config, data, flatpak, executables = host_info()
    browsers = installed_browsers() if browsers is None else browsers
    if not browsers:
        raise SetupError("noBrowser")
    directory = data / "cassini/native-hosts" / target.application
    template = Path(__file__).with_name("flatpak-native-host.sh").read_text()
    hosts = {}
    for name, command in (
        ("io.codemyriad.cassini", "cassini-browser-host"),
        ("io.codemyriad.cassini.control", "cassini-browser-control-host"),
    ):
        content = template
        for key, value in dict(
            FLATPAK=flatpak, APPLICATION=target.application, ARCH=target.arch, BRANCH=target.branch, COMMAND=command
        ).items():
            content = content.replace("@" + key + "@", shlex.quote(value))
        executable = directory / command
        _write(executable, content, "700")
        hosts[name] = executable
    paths = [browser.config / "NativeMessagingHosts" / (name + ".json") for browser in browsers for name in hosts]
    previous = _registration_files(paths, allow_unsafe=True)
    written, errors = [], []
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
            try:
                if isinstance(previous[destination], Exception):
                    raise previous[destination]
                replace, dead = decision(
                    previous[destination],
                    manifest,
                    force=force,
                    protected=protected_build(),
                    target_ready=_registered_host_ready,
                )
                from .registration import manifest as read_manifest

                if replace and not dead and read_manifest(previous[destination]) == manifest:
                    written.append(destination)
                    continue
                if replace:
                    import hashlib

                    expected = (
                        hashlib.sha256(previous[destination]).hexdigest()
                        if previous[destination] is not None
                        else "missing"
                    )
                    _write(destination, json.dumps(manifest, indent=2) + "\n", "600", expected=expected)
                    if dead:
                        from .installed_build import lifecycle_log

                        lifecycle_log("Repaired a dead browser companion registration: %s", destination)
                written.append(destination)
            except SetupError as error:
                if error.retryable:
                    raise
                errors.append(error)
    _cleanup_legacy_directories([browser.config / "NativeMessagingHosts" for browser in browsers])
    if errors:
        raise errors[0]
    return written


def _registration_files(paths, *, allow_unsafe=False):
    """Bounded snapshot of known host files; native writes are atomic too."""
    from .companion import SetupError

    paths = list(paths)
    script = r"""set -eu
for path in "$@"; do
 if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
  printf 'unsafe\0\0'; continue
 fi
 if [ -e "$path" ]; then
  printf 'present\0'; head -c 65537 -- "$path"
 else printf 'missing\0'; fi
 printf '\0'
done"""
    try:
        result = subprocess.run(
            host_command(["/bin/sh", "-c", script, "cassini-registrations", *map(str, paths)], watch=True),
            capture_output=True,
            check=True,
            timeout=3,
        )
        if len(result.stdout) > len(paths) * 65548:
            raise SetupError("unsafe")
        parts = result.stdout.split(b"\0")
        if len(parts) != len(paths) * 2 + 1 or parts[-1] != b"":
            raise SetupError("unsafe")
        files = {}
        for path, mode, data in zip(paths, parts[0:-1:2], parts[1:-1:2], strict=True):
            if mode not in (b"present", b"missing", b"unsafe"):
                raise SetupError("unsafe")
            if mode == b"unsafe" or len(data) > 65536:
                if not allow_unsafe:
                    raise SetupError("unsafe")
                files[path] = SetupError("unsafe")
            else:
                files[path] = data if mode == b"present" else None
        return files
    except subprocess.CalledProcessError as error:
        raise SetupError(
            "unsafe" if error.returncode == 78 else "temporary", retryable=error.returncode != 78
        ) from error
    except subprocess.SubprocessError as error:
        raise SetupError("temporary", retryable=True) from error


def _registered_host_ready(path):
    from .registration import APPLICATIONS, installed_relay, relay_target

    def run(command, **kwargs):
        return subprocess.run(host_command(command, watch=True), **kwargs)

    result = run(
        ["/bin/sh", "-c", '[ -f "$1" ] && [ -x "$1" ]', "cassini-registration-target", str(path)],
        capture_output=True,
        timeout=2,
    )
    if result.returncode:
        return False
    path = Path(path)
    if path.parent.name not in APPLICATIONS or path.parent.parent.name != "native-hosts":
        return True
    target = relay_target(path, _registration_files([path])[path] or b"")
    if target is False:
        raise OSError("Could not identify the registered Flatpak relay")
    if target is None:
        return True
    _, _, flatpak, _ = host_info()
    return installed_relay(target, run=run, flatpak=flatpak)


def registration_health(browsers):
    from .registration import manifest

    paths = [
        browser.config / "NativeMessagingHosts" / (name + ".json")
        for browser in browsers
        for name in ("io.codemyriad.cassini", "io.codemyriad.cassini.control")
    ]
    ready = {}
    for raw in _registration_files(paths).values():
        value = manifest(raw)
        if value is None:
            return True
        path = value["path"]
        if path not in ready:
            ready[path] = _registered_host_ready(path)
        if not ready[path]:
            return True
    return False
