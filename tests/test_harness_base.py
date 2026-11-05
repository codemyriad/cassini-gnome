# SPDX-License-Identifier: GPL-3.0-or-later
"""C (D68): a cached harness image and a fresh build stand on the same base.

tests/harness-image.sh pins each harness's base image to the digest of this
platform's manifest, hashes the pin into the harness tag (and so into CI's
cache key) and every run.sh builds the harness stage FROM that same pin. A base
that moves on the registry within a week is a new tag, never a cache hit on the
old base. Docker is a fake here: no daemon, no registry.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HARNESS_IMAGES = {
    "desktop": "tests/desktop/Dockerfile",
    "build-refresh": "tests/build-refresh/Dockerfile",
    "browser-setup": "tests/browser-setup/Dockerfile",
    "whistle": "tests/whistle/Containerfile",
}
# Every run.sh that builds a harness stage, and the harness it builds.
RUNNERS = {
    "tests/desktop/run.sh": "desktop",
    "tests/build-refresh/run.sh": "build-refresh",
    "tests/browser-setup/run.sh": "browser-setup",
    "tests/whistle/run.sh": "whistle",
    "tests/parakeet/run.sh": "whistle",
    "tests/diarization/run.sh": "whistle",
    "tests/voices/run.sh": "whistle",
}
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
EPOCH = "2026-W45"

# A docker that logs every call and answers like a daemon with registry access.
# Each registry lookup serves a new digest, as a base moving between two lookups would.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import hashlib, json, os, sys
state = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["version"]:
    print(os.environ.get("FAKE_DOCKER_PLATFORM", "linux/amd64"))
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    if os.environ.get("FAKE_REGISTRY_DOWN"):
        sys.exit("ERROR: 429 Too Many Requests")
    lookups = os.path.join(state, "lookups.jsonl")
    count = sum(1 for _ in open(lookups)) if os.path.exists(lookups) else 0
    reference = args[-1]
    def digest(what):
        return "sha256:" + hashlib.sha256(f"{reference} {what} {count}".encode()).hexdigest()
    platforms = [("linux", "amd64", None), ("unknown", "unknown", None), ("linux", "arm64", "v8")]
    if os.environ.get("FAKE_REGISTRY_PLATFORMS") == "arm-only":
        platforms = platforms[1:]
    manifests = [
        {"digest": digest(f"{os_}/{arch}"), "platform": {"os": os_, "architecture": arch, **({"variant": v} if v else {})}}
        for os_, arch, v in platforms
    ]
    with open(lookups, "a") as log:
        log.write(json.dumps({"reference": reference, "manifests": manifests}) + "\n")
    print(json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest("index"), "manifests": manifests}))
elif args[:2] == ["image", "inspect"]:
    if "-f" in args:
        print("null")
    else:
        sys.exit(1)  # no harness image yet: run.sh builds it
elif args[:1] == ["build"] and "-q" in args:
    print("sha256:" + "0" * 64)
"""
# The Playwright lookup browser-setup/run.sh makes, answered with a fixture path.
FAKE_NODE = """#!/bin/sh
echo "$FAKE_BROWSER"
"""


def pinned(*pins):
    return " ".join(pins)


def run(args, env, check=True):
    return subprocess.run(
        args, env={"PATH": os.environ["PATH"], **env}, check=check, capture_output=True, text=True, cwd=ROOT
    )


def harness(*args, **env):
    return run(["bash", str(ROOT / "tests/harness-image.sh"), *args], env).stdout.strip()


def cache_key(*names, **env):
    out = run(["bash", str(ROOT / "build-aux/ci/harness-cache.sh"), "key", *names], env).stdout
    return dict(line.split("=", 1) for line in out.splitlines())["key"]


@pytest.fixture
def docker(tmp_path):
    """A fake docker (and node) first on PATH; its log, lookups and the env to use it."""
    bin_dir, state = tmp_path / "bin", tmp_path / "docker-state"
    bin_dir.mkdir()
    state.mkdir()
    for name, text in (("docker", FAKE_DOCKER), ("node", FAKE_NODE)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    browser = tmp_path / "browsers/chromium-1/chrome-linux/chrome"
    browser.parent.mkdir(parents=True)
    browser.write_text("")
    browser.chmod(0o755)

    class Docker:
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_BROWSER": str(browser),
            "HOME": str(tmp_path),
            "CASSINI_HARNESS_EPOCH": EPOCH,
        }

        @staticmethod
        def calls():
            path = state / "calls.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        @staticmethod
        def lookups():
            path = state / "lookups.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        @staticmethod
        def amd64(lookup):
            (digest,) = [m["digest"] for m in lookup["manifests"] if m["platform"]["architecture"] == "amd64"]
            return f"{lookup['reference']}@{digest}"

    return Docker


def test_a_moved_base_alone_changes_the_tag_and_the_cache_key(docker):
    # The review's finding: ubuntu:26.04 moved within a week, and a pull request
    # restored the harness built on the old base. Only the digest changes here.
    a = {**docker.env, "CASSINI_HARNESS_BASES": f"ubuntu:26.04@{DIGEST_A}"}
    b = {**docker.env, "CASSINI_HARNESS_BASES": f"ubuntu:26.04@{DIGEST_B}"}
    for name in HARNESS_IMAGES:
        assert harness("tag", name, **a) == harness("tag", name, **a)
        assert harness("tag", name, **a) != harness("tag", name, **b), name
        assert re.fullmatch(rf"cassini-harness-{name}:[0-9a-f]{{16}}-{EPOCH}", harness("tag", name, **b))
    for names in (["desktop"], ["whistle"], ["build-refresh", "browser-setup"]):
        assert cache_key(*names, **a) == cache_key(*names, **a)
        assert cache_key(*names, **a) != cache_key(*names, **b), names
    # browser-setup's own base: its pin, and only its pin, moves its tag.
    debian = {**docker.env, "CASSINI_BROWSER_TEST_BASE": "debian:testing"}
    tag = harness("tag", "browser-setup", **debian, CASSINI_HARNESS_BASES=f"debian:testing@{DIGEST_A}")
    assert tag != harness("tag", "browser-setup", **debian, CASSINI_HARNESS_BASES=f"debian:testing@{DIGEST_B}")
    both = pinned(f"ubuntu:26.04@{DIGEST_B}", f"debian:testing@{DIGEST_A}")
    assert tag == harness("tag", "browser-setup", **debian, CASSINI_HARNESS_BASES=both)
    # A pinned base is never looked up.
    assert docker.lookups() == [] and not any(call[:1] == ["version"] for call in docker.calls())


def test_an_unpinned_base_is_this_platforms_digest_or_an_error(docker):
    for name in HARNESS_IMAGES:
        base = harness("base", name, **docker.env)
        assert base == docker.amd64(docker.lookups()[-1]) and base.startswith("ubuntu:26.04@sha256:"), name
    assert harness("base", "desktop", **docker.env, FAKE_DOCKER_PLATFORM="linux/arm64").endswith(
        [m for m in docker.lookups()[-1]["manifests"] if m["platform"]["architecture"] == "arm64"][0]["digest"]
    )
    assert harness("base", "browser-setup", **docker.env, CASSINI_BROWSER_TEST_BASE="debian:testing").startswith(
        "debian:testing@sha256:"
    )
    # pin: what CASSINI_HARNESS_BASES held, plus each new image once.
    extra = f"registry.example/other:1@{DIGEST_A}"
    pins = harness("pin", "desktop", "whistle", "browser-setup", **docker.env, CASSINI_HARNESS_BASES=extra).split(" ")
    assert pins[0] == extra and len(pins) == 2 and pins[1] == docker.amd64(docker.lookups()[-1])
    # No digest, no tag: never a silent fallback to the bare tag.
    for env in ({"FAKE_REGISTRY_DOWN": "1"}, {"FAKE_REGISTRY_PLATFORMS": "arm-only"}):
        for args in (["base", "desktop"], ["tag", "desktop"], ["pin", "desktop"]):
            failed = run(["bash", str(ROOT / "tests/harness-image.sh"), *args], {**docker.env, **env}, check=False)
            assert failed.returncode != 0 and failed.stdout == "", (args, env)
            assert "cannot resolve the linux/amd64 digest of ubuntu:26.04" in failed.stderr
    down = run(
        ["bash", str(ROOT / "build-aux/ci/harness-cache.sh"), "key", "desktop"],
        {**docker.env, "FAKE_REGISTRY_DOWN": "1"},
        check=False,
    )
    assert down.returncode != 0 and "key=" not in down.stdout
    for bad in ("ubuntu:26.04", "ubuntu:26.04@sha256:abc", f"ubuntu:26.04@{DIGEST_A.upper()}", f"@{DIGEST_A}"):
        failed = run(
            ["bash", str(ROOT / "tests/harness-image.sh"), "tag", "desktop"],
            {**docker.env, "CASSINI_HARNESS_BASES": bad},
            check=False,
        )
        assert failed.returncode == 2 and "CASSINI_HARNESS_BASES" in failed.stderr, bad


def test_every_harness_stage_builds_from_the_base_the_tag_pins():
    for name, path in HARNESS_IMAGES.items():
        text = (ROOT / path).read_text()
        froms = re.findall(r"^FROM .*$", text, re.M)
        # The image ARG BASE names is what harness-image.sh pins; nothing else is a base.
        assert froms == ["FROM ${BASE} AS harness", "FROM ${HARNESS}"], (name, froms)
        assert re.findall(r"^ARG BASE=(.*)$", text.split("\nFROM ", 1)[0], re.M) == ["ubuntu:26.04"], name
    for script, name in RUNNERS.items():
        text = re.sub(r"\\\n\s*", " ", (ROOT / script).read_text())
        root = re.search(r'"\$(\w+)/tests/harness-image\.sh" tag ', text)[1]
        tool = f'"${root}/tests/harness-image.sh"'
        # Pinned once, exported so the tag and the prune see the same pin, and only then named.
        pin = f"export CASSINI_HARNESS_BASES\nCASSINI_HARNESS_BASES=$({tool} pin {name})\nbase=$({tool} base {name})\n"
        assert f"{pin}harness=$({tool} tag {name})\n" in text, script
        builds = [line for line in text.splitlines() if re.search(r"(^|[\s(])docker build\s", line)]
        stage = [line for line in builds if "--target harness" in line]
        assert len(stage) == 1 and '--build-arg BASE="$base"' in stage[0], script
        assert not any("--build-arg BASE" in line for line in builds if line not in stage), script
        assert "imagetools" not in text and "CASSINI_BROWSER_TEST_BASE:-" not in text, script


def fixture_args(script, tmp_path):
    """Arguments run.sh accepts, all made up: the harness build is what is under test."""
    tree = tmp_path / "fixture"
    modules = tree / "share/cassini_talk"
    bundle, stage, out = tree / "share/whistle", tree / "stage", tmp_path / "out"
    for directory in (bundle, modules, stage / "ort/onnxruntime", stage / "sherpa", tree / "models"):
        directory.mkdir(parents=True, exist_ok=True)
    (modules / "paths.json").write_text(json.dumps({"onnxruntime": "/ort", "sherpa": "/sherpa"}))
    for name in ("whistle_client.py", "parakeet_client.py"):
        (modules / name).write_text("")
    for name in ("app.deb", "app.flatpak", "input.wav"):
        (tree / name).write_text(name)
    deb, flatpak, models, audio = tree / "app.deb", tree / "app.flatpak", tree / "models", tree / "input.wav"
    return {
        "tests/desktop/run.sh": [deb, flatpak, out],
        "tests/build-refresh/run.sh": [deb, out],
        "tests/browser-setup/run.sh": [deb, out],
        "tests/whistle/run.sh": [bundle, out, stage],
        "tests/parakeet/run.sh": [bundle, models, stage / "sherpa", out],
        "tests/diarization/run.sh": [bundle, models, audio, out, stage, models],
        "tests/voices/run.sh": [bundle, models, audio, out, stage],
    }[script]


def harness_build(calls):
    (build,) = [call for call in calls if call[:1] == ["build"] and "--target" in call]
    arg = build.index("--build-arg")
    return build[build.index("-t") + 1], build[arg + 1]


@pytest.mark.parametrize("script", sorted(RUNNERS))
def test_each_run_builds_the_harness_from_one_resolution_of_its_base(script, tmp_path, docker):
    name = RUNNERS[script]
    args = [str(a) for a in fixture_args(script, tmp_path)]
    # Locally: one lookup, and the tag hashes the very digest the build is FROM.
    run(["bash", str(ROOT / script), *args], docker.env)
    (lookup,) = docker.lookups()
    tag, base = harness_build(docker.calls())
    assert base == f"BASE={docker.amd64(lookup)}"
    assert tag == harness("tag", name, **docker.env, CASSINI_HARNESS_BASES=docker.amd64(lookup))
    # In CI: the cache key's step pins the job's bases, and run.sh looks nothing up.
    shutil.rmtree(tmp_path / "docker-state")
    (tmp_path / "docker-state").mkdir()
    github_env = tmp_path / "github-env"
    key = cache_key(name, **docker.env, GITHUB_ENV=str(github_env))
    (lookup,) = docker.lookups()
    job = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert job == {"CASSINI_HARNESS_EPOCH": EPOCH, "CASSINI_HARNESS_BASES": docker.amd64(lookup)}
    run(["bash", str(ROOT / script), *args], {**docker.env, **job})
    assert len(docker.lookups()) == 1, "run.sh looked the base up again"
    tag, base = harness_build(docker.calls())
    assert base == f"BASE={job['CASSINI_HARNESS_BASES']}"
    tags = harness("tag", name, **{**docker.env, **job})
    assert tag == tags
    assert key.endswith(hashlib.sha256(f"{tags}\n".encode()).hexdigest()[:16])
    assert len(docker.lookups()) == 1


def test_the_cache_key_resolves_each_base_once_for_the_whole_job(docker, tmp_path):
    github_env = tmp_path / "github-env"
    cache_key("build-refresh", "browser-setup", **docker.env, GITHUB_ENV=str(github_env))
    (lookup,) = docker.lookups()
    job = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert job["CASSINI_HARNESS_BASES"] == docker.amd64(lookup)
    # Saving after the run names the same tags, with no further lookup.
    for name in ("build-refresh", "browser-setup"):
        harness("tag", name, **{**docker.env, **job})
    assert len(docker.lookups()) == 1
