#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Guard rpm-canary.yml when fedora:latest ships a Python ABI upstream onnxruntime lacks.

outcomes:
  - abi pinned in both whistle/pins.json and cassini-talk.spec -> should_build=true, exit 0
  - abi pinned in only one of pins.json or cassini-talk.spec -> ::error::, exit 1
  - abi unpinned and upstream PyPI onnxruntime publishes a non-yanked manylinux x86_64
    wheel for abi (at version >= pinned version) -> ::error::, exit 1 (actionable alert)
  - abi unpinned and upstream PyPI onnxruntime has no wheel for abi yet ->
    should_build=false, ::notice:: + step summary, exit 0
"""

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/onnxruntime/json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
ABI_RE = re.compile(r"^cp[0-9]{3,}$")
SPEC_ABI_RE = re.compile(r'^%if "%\{\?python3_version_nodots\}" == "(\d+)"$', re.M)


class TransientPyPIError(RuntimeError):
    """Bounded retryable upstream PyPI CDN/DNS/timeout failure."""


def version_key(version: str) -> tuple[int, ...] | None:
    """Parse strictly numeric X.Y.Z release strings; skip pre-releases/dev tags."""
    if not re.fullmatch(r"\d+(?:\.\d+)+", version):
        return None
    return tuple(int(part) for part in version.split("."))


def fetch_pypi_json(url: str = PYPI_URL, timeout: float = 10.0, max_attempts: int = 3, sleep_fn=time.sleep) -> dict:
    """Fetch PyPI JSON with bounded retries for HTTP 429/5xx, DNS, and socket timeouts."""
    last_error = "unknown upstream error"
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cassini-rpm-canary/1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                expected_header = resp.headers.get("Content-Length")
                if expected_header is not None:
                    expected_len = int(expected_header)
                    if expected_len < 0 or expected_len > MAX_RESPONSE_BYTES:
                        raise ValueError(f"PyPI Content-Length {expected_len} out of bounds")
                    body = resp.read(expected_len)
                    if len(body) != expected_len:
                        raise TransientPyPIError(f"truncated PyPI response ({len(body)} != {expected_len} bytes)")
                else:
                    body = resp.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError(f"PyPI response exceeded {MAX_RESPONSE_BYTES} bytes")
                data = json.loads(body.decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
                    raise ValueError("PyPI JSON missing 'releases' mapping")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_HTTP_CODES:
                last_error = f"HTTP {exc.code}"
            else:
                raise RuntimeError(f"PyPI returned non-retryable HTTP {exc.code}") from exc
        except TimeoutError as exc:
            last_error = f"timeout ({exc})"
        except (urllib.error.URLError, http.client.IncompleteRead, TransientPyPIError) as exc:
            last_error = f"connection/read error ({exc})"
        except json.JSONDecodeError as exc:
            last_error = f"truncated or invalid JSON ({exc})"

        if attempt < max_attempts:
            sleep_fn(2.0 * attempt)

    raise TransientPyPIError(f"PyPI unreachable after {max_attempts} bounded attempts ({last_error})")


def find_upstream_wheel(pypi_data: dict, abi: str, min_version: str) -> tuple[str, str] | None:
    """Return (version, filename) of the newest matching non-yanked manylinux x86_64 wheel, if any."""
    floor = version_key(min_version) or (0,)
    candidates: list[tuple[tuple[int, ...], str, str]] = []
    wheel_re = re.compile(rf"^onnxruntime-[^-]+-{re.escape(abi)}-{re.escape(abi)}-.*manylinux.*x86_64\.whl$")
    for ver, files in pypi_data.get("releases", {}).items():
        parsed = version_key(ver)
        if parsed is None or parsed < floor or not isinstance(files, list):
            continue
        for entry in files:
            if not isinstance(entry, dict) or entry.get("yanked"):
                continue
            filename = entry.get("filename", "")
            if wheel_re.match(filename):
                candidates.append((parsed, ver, filename))
    if not candidates:
        return None
    _, best_ver, best_file = max(candidates)
    return best_ver, best_file


def write_line(path: Path | None, text: str) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def evaluate(
    abi: str,
    pins_path: Path,
    spec_path: Path,
    github_output: Path | None = None,
    step_summary: Path | None = None,
    fetcher=fetch_pypi_json,
) -> int:
    abi = abi.strip()
    if not ABI_RE.match(abi):
        print(f"::error::Invalid Python ABI tag from fedora:latest probe: {abi!r}", file=sys.stderr)
        return 1

    ort = json.loads(pins_path.read_text(encoding="utf-8"))["onnxruntime"]
    pinned_version = ort["version"]
    pinned_abis = set(ort["wheels"])
    spec_abis = {f"cp{nodots}" for nodots in SPEC_ABI_RE.findall(spec_path.read_text(encoding="utf-8"))}

    in_pins = abi in pinned_abis
    in_spec = abi in spec_abis
    if in_pins and in_spec:
        write_line(github_output, "should_build=true")
        print(f"{abi} is pinned in whistle/pins.json and cassini-talk.spec; running RPM canary build.")
        return 0
    if in_pins != in_spec:
        print(
            f"::error::{abi} pin mismatch between whistle/pins.json ({in_pins}) and cassini-talk.spec ({in_spec})",
            file=sys.stderr,
        )
        return 1

    try:
        pypi_data = fetcher()
    except TransientPyPIError as exc:
        write_line(github_output, "should_build=false")
        msg = (
            f"fedora:latest uses {abi} (unpinned in whistle/pins.json), and PyPI wheel check failed "
            f"({exc}); skipping RPM build."
        )
        print(f"::warning::{msg}")
        write_line(step_summary, f"### RPM canary skipped (`{abi}`)\n\n{msg}")
        return 0

    found = find_upstream_wheel(pypi_data, abi, pinned_version)
    if found is not None:
        ver, filename = found
        msg = (
            f"fedora:latest uses {abi}, and upstream onnxruntime {ver} now publishes {filename} on PyPI. "
            f"Pin {abi} in whistle/pins.json and build-aux/rpm/cassini-talk.spec so packages.yml can return to fedora:latest."
        )
        print(f"::error::{msg}", file=sys.stderr)
        write_line(step_summary, f"### Action required: `{abi}` wheel now on PyPI\n\n{msg}")
        return 1

    write_line(github_output, "should_build=false")
    msg = (
        f"Skipped fedora:latest RPM build (packages.yml stays on fedora:44): fedora:latest ships {abi}, "
        f"and upstream onnxruntime (>={pinned_version}) has no {abi} manylinux x86_64 wheel on PyPI yet."
    )
    print(f"::notice::{msg}")
    write_line(step_summary, f"### RPM canary skipped (`{abi}`)\n\n{msg}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--abi", required=True, help="Container Python ABI tag, e.g. cp315")
    parser.add_argument("--pins", type=Path, default=Path("whistle/pins.json"))
    parser.add_argument("--spec", type=Path, default=Path("build-aux/rpm/cassini-talk.spec"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--step-summary", type=Path)
    args = parser.parse_args(argv)
    return evaluate(
        args.abi,
        args.pins,
        args.spec,
        github_output=args.github_output,
        step_summary=args.step_summary,
    )


if __name__ == "__main__":
    raise SystemExit(main())
