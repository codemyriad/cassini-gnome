# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
from pathlib import Path


def test_development_profile_registers_both_hosts_for_loaded_extension(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("profile_setup", root / "build-aux/prepare-browser-profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = tmp_path / "profile"
    recordings = tmp_path / "recordings"
    identifier = module.prepare(root, profile, recordings)
    assert identifier == "pcafdaahfhjiolfpfnlhmljfgoabbaij"
    for name, module_name in [
        ("com.codemyriad.cassini", "browser_host"),
        ("com.codemyriad.cassini.control", "browser_control_host"),
    ]:
        manifest = json.loads((profile / "NativeMessagingHosts" / f"{name}.json").read_text())
        assert manifest["name"] == name
        assert manifest["allowed_origins"] == [f"chrome-extension://{identifier}/"]
        program = Path(manifest["path"])
        assert program.stat().st_mode & 0o777 == 0o700
        source = program.read_text()
        assert f"from cassini_viewer.{module_name} import main" in source
        if module_name == "browser_host":
            assert repr(str(recordings)) in source
