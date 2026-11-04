# SPDX-License-Identifier: GPL-3.0-or-later
"""G15: the tag workflow ships the Flatpak only when its signed remote serves it.

Every case runs build-aux/ci/flatpak_bundle.py against a copy of a real signed
repository and real bundles made by flatpak and ostree with throwaway keys
(tests/flatpak/make-release-fixture.sh). Only the transport is replaced: the
D45.1 URL is served from the copy, so a test can unpublish or tamper with it.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/flatpak/release-fixture"
APP = "app/io.codemyriad.CassiniTalk/x86_64/master"

pytestmark = pytest.mark.skipif(shutil.which("gpgv") is None, reason="gpgv verifies the signatures")


@pytest.fixture
def guard():
    pytest.importorskip("gi")
    spec = importlib.util.spec_from_file_location("flatpak_bundle", ROOT / "build-aux/ci/flatpak_bundle.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def remote(tmp_path, guard):
    """The published repository: a copy the test may change, served at the D45.1 URL."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE / "repo", repo)

    def get(url):
        assert url.startswith(guard.URL), url
        path = repo / url[len(guard.URL) :]
        return path.read_bytes() if path.is_file() else None

    return repo, get


def commit_of(guard, name):
    return guard.read_bundle(FIXTURE / name)[1]


def rebundle(guard, tmp_path, name, **changes):
    """A real bundle with some metadata entries replaced (None removes one)."""
    from gi.repository import GLib

    value = guard.variant(guard.SUPERBLOCK, (FIXTURE / name).read_bytes())
    metadata = value.get_child_value(0)
    entries = {}
    for index in range(metadata.n_children()):
        entry = metadata.get_child_value(index)
        entries[entry.get_child_value(0).get_string()] = entry.get_child_value(1).get_variant()
    for key, replacement in changes.items():
        if replacement is None:
            entries.pop(key)
        else:
            entries[key] = replacement
    children = [GLib.Variant("a{sv}", entries)] + [value.get_child_value(i) for i in range(1, value.n_children())]
    path = tmp_path / ("changed-" + name)
    path.write_bytes(GLib.Variant.new_tuple(*children).get_data_as_bytes().get_data())
    return path


def refused(guard, remote, path, match):
    with pytest.raises(guard.Refused, match=match):
        guard.check(path, get=remote[1])


def test_the_current_bundle_passes_when_its_signed_remote_serves_it(guard, remote):
    assert guard.check(FIXTURE / "current.flatpak", get=remote[1]) == (APP, commit_of(guard, "current.flatpak"))


def test_a_bundle_older_than_the_published_commit_is_refused(guard, remote):
    assert commit_of(guard, "stale.flatpak") != commit_of(guard, "current.flatpak")
    refused(guard, remote, FIXTURE / "stale.flatpak", "not the bundle's commit")


def test_an_unpublished_remote_is_refused(guard, tmp_path):
    with pytest.raises(guard.Refused, match="serves no summary"):
        guard.check(FIXTURE / "current.flatpak", get=lambda url: None)


@pytest.mark.parametrize(
    ("files", "match"),
    [
        (["summary.sig"], "summary is not signed"),
        (["summary.idx.sig"], "summary.idx is not signed"),
        (["summary", "summary.sig"], None),  # The index alone is enough when it is signed.
        (["summary.idx", "summary.idx.sig"], None),
        (["summary", "summary.sig", "summary.idx", "summary.idx.sig"], "serves no summary"),
    ],
)
def test_every_summary_the_remote_serves_must_be_signed(guard, remote, files, match):
    for name in files:
        (remote[0] / name).unlink()
    if match is None:
        assert guard.check(FIXTURE / "current.flatpak", get=remote[1])[0] == APP
    else:
        refused(guard, remote, FIXTURE / "current.flatpak", match)


def flip(path, offset=-40):
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


def test_tampered_summaries_or_subsummaries_are_refused(guard, remote):
    repo = remote[0]
    original = (repo / "summary").read_bytes()
    flip(repo / "summary")
    refused(guard, remote, FIXTURE / "current.flatpak", "summary: no valid signature")
    (repo / "summary").write_bytes(original)
    flip(repo / "summary.idx")
    refused(guard, remote, FIXTURE / "current.flatpak", r"summary\.idx: no valid signature")


def test_a_subsummary_that_does_not_match_the_signed_index_is_refused(guard, remote):
    repo = remote[0]
    (packed,) = [p for p in (repo / "summaries").glob("*.gz") if p.name.startswith(_subsummary(guard, repo))]
    other = next(p for p in (repo / "summaries").glob("*.gz") if p != packed)
    shutil.copyfile(other, packed)  # A real, older subsummary under the signed digest.
    refused(guard, remote, FIXTURE / "current.flatpak", "does not match the digest")
    packed.unlink()
    refused(guard, remote, FIXTURE / "current.flatpak", "is missing")


def _subsummary(guard, repo):
    index = guard.variant(guard.INDEX, (repo / "summary.idx").read_bytes()).unpack()[0]
    return bytes(index["x86_64"][0]).hex()


def test_an_unsigned_unpublished_or_tampered_commit_is_refused(guard, remote):
    commit = commit_of(guard, "current.flatpak")
    objects = remote[0] / "objects" / commit[:2]
    (objects / (commit[2:] + ".commitmeta")).unlink()
    refused(guard, remote, FIXTURE / "current.flatpak", "is not signed")
    shutil.copyfile(
        FIXTURE / "repo/objects" / commit[:2] / (commit[2:] + ".commitmeta"), objects / (commit[2:] + ".commitmeta")
    )
    flip(objects / (commit[2:] + ".commit"))
    refused(guard, remote, FIXTURE / "current.flatpak", "is not published")
    (objects / (commit[2:] + ".commit")).unlink()
    refused(guard, remote, FIXTURE / "current.flatpak", "is not published")


def test_signatures_must_be_by_the_key_the_bundle_carries(guard, remote, tmp_path):
    from gi.repository import GLib

    other = GLib.Variant("ay", (FIXTURE / "other.gpg").read_bytes())
    path = rebundle(guard, tmp_path, "current.flatpak", **{"gpg-keys": other})
    refused(guard, remote, path, "no valid signature by the bundled key")


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"origin": None}, "no update remote"),
        ({"origin": "https://example.invalid/repo/"}, "no update remote"),
        ({"gpg-keys": None}, "no public key"),
        ({"ref": "app/io.codemyriad.Other/x86_64/master"}, "not the bundle's commit"),
        ({"ref": "app/io.codemyriad.CassiniTalk/aarch64/master"}, "no aarch64 subsummary"),
    ],
)
def test_a_bundle_without_the_d45_remote_or_with_another_ref_is_refused(guard, remote, tmp_path, changes, match):
    from gi.repository import GLib

    values = {k: None if v is None else GLib.Variant("s", v) for k, v in changes.items()}
    refused(guard, remote, rebundle(guard, tmp_path, "current.flatpak", **values), match)


def test_main_reports_a_refusal_as_an_error_annotation(guard, tmp_path, capsys):
    # Like the bundle main builds today: no origin, so it is refused before any fetch.
    path = rebundle(guard, tmp_path, "current.flatpak", origin=None)
    assert guard.main([str(path)]) == 1
    assert capsys.readouterr().out.startswith(f"::error::{path}: no update remote")
