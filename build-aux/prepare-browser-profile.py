#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare both native hosts for a disposable development browser profile."""

import argparse
import hashlib
import json
from pathlib import Path


def prepare(root, profile, recordings):
    root, profile, recordings = Path(root).resolve(), Path(profile).resolve(), Path(recordings).resolve()
    extension = root / "browser-extension"
    digest = hashlib.sha256(str(extension).encode()).hexdigest()[:32]
    identifier = "".join(chr(ord("a") + int(value, 16)) for value in digest)
    hosts = profile / "NativeMessagingHosts"
    hosts.mkdir(parents=True, exist_ok=True)
    scripts = profile.parent / "hosts"
    scripts.mkdir(mode=0o700, parents=True, exist_ok=True)
    scripts.chmod(0o700)
    for name, module, arguments in [
        ("com.codemyriad.cassini", "browser_host", f"directory={str(recordings)!r}, open_completed=False"),
        ("com.codemyriad.cassini.control", "browser_control_host", ""),
    ]:
        script = scripts / f"{name}.py"
        script.write_text(
            "#!/usr/bin/python3\nimport sys\n"
            f"sys.path.insert(0, {str(root / 'src')!r})\n"
            f"from cassini_viewer.{module} import main\n"
            f"sys.exit(main({arguments}))\n"
        )
        script.chmod(0o700)
        (hosts / f"{name}.json").write_text(
            json.dumps(
                dict(
                    name=name,
                    description="Cassini development browser",
                    path=str(script),
                    type="stdio",
                    allowed_origins=[f"chrome-extension://{identifier}/"],
                ),
                indent=2,
            )
            + "\n"
        )
    return identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--recordings", required=True)
    args = parser.parse_args()
    identifier = prepare(Path(__file__).resolve().parents[1], args.profile, args.recordings)
    print(f"Cassini extension and both native hosts ready: {identifier}")


if __name__ == "__main__":
    main()
