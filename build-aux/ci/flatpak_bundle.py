#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Refuse to release a Flatpak bundle its users could not update (G15, D45.1).

    build-aux/ci/flatpak_bundle.py cassini-talk.flatpak

Runs in the tag workflow only, never in PR or main CI. Main builds the stable
bundle without an update remote until the D45.1 repository is published, and
the release reuses exactly that bundle, so this refuses it until then. A bundle
passes only when the published repository is the signed remote it names:

- the bundle's origin is the D45.1 URL and it carries a public key;
- every summary the remote serves (the legacy `summary`, and `summary.idx`
  with this architecture's subsummary, whose digest the index pins) has a
  detached signature that gpgv accepts with the bundled key alone;
- each of them lists the bundle's ref at the bundle's own commit; and
- that commit object is published, hashes to its name, and its detached
  signature verifies with the bundled key.

A missing remote, summary, signature or commit, another key's signature, or a
stale commit is refused. tests/test_flatpak_release_guard.py runs this against
a real signed repository made by flatpak and ostree (tests/flatpak/).
"""

import argparse
import gzip
import hashlib
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://cassini.talk/download/linux/flatpak/"
# OSTree's static-delta superblock, which `flatpak build-bundle` writes: the
# bundle metadata (ref, origin, gpg-keys, ...), the target commit's checksum
# and the commit object itself.
SUPERBLOCK = "(a{sv}tayay(a{sv}aya(say)sstayay)aya(uayttay)a(yaytt))"
SUMMARY = "(a(s(taya{sv}))a{sv})"
INDEX = "(a{s(ayaaya{sv})}a{sv})"
DETACHED = "a{sv}"


class Refused(Exception):
    pass


def variant(kind, data):
    from gi.repository import GLib

    return GLib.Variant.new_from_bytes(GLib.VariantType.new(kind), GLib.Bytes.new(data), False)


def read_bundle(path):
    value = variant(SUPERBLOCK, Path(path).read_bytes())
    commit = bytes(value.get_child_value(4).get_data_as_bytes().get_data())
    checksum = bytes(value.get_child_value(3).unpack()).hex()
    if hashlib.sha256(commit).hexdigest() != checksum:
        raise Refused(f"the bundle's commit does not hash to its checksum {checksum}")
    return value.get_child_value(0).unpack(), checksum


def fetch(url):
    """The bytes at url, or None when the server says it is not there."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Refused(f"{url}: HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise Refused(f"{url}: {error}") from error


def verify(data, detached, keyring, what):
    """At least one detached OpenPGP signature over data by a key in keyring."""
    if detached is None:
        raise Refused(f"{what} is not signed")
    signatures = variant(DETACHED, detached).unpack().get("ostree.gpgsigs") or []
    if not signatures:
        raise Refused(f"{what} carries no OpenPGP signature")
    with tempfile.TemporaryDirectory(prefix="cassini-verify-") as directory:
        home, signed = Path(directory), Path(directory) / "signed"
        signed.write_bytes(data)
        for index, signature in enumerate(signatures):
            sig = home / f"{index}.sig"
            sig.write_bytes(bytes(signature))
            status = subprocess.run(
                ["gpgv", "--homedir", str(home), "--status-fd", "1", "--keyring", str(keyring), str(sig), str(signed)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if status.returncode == 0 and "[GNUPG:] VALIDSIG " in status.stdout:
                return
    raise Refused(f"{what}: no valid signature by the bundled key")


def refs(summary):
    return {ref: bytes(checksum).hex() for ref, (_size, checksum, _meta) in variant(SUMMARY, summary).unpack()[0]}


def summaries(origin, arch, keyring, get):
    """{name: {ref: commit}} for every signed summary the remote serves."""
    found = {}
    if (legacy := get(origin + "summary")) is not None:
        verify(legacy, get(origin + "summary.sig"), keyring, "summary")
        found["summary"] = refs(legacy)
    if (index := get(origin + "summary.idx")) is not None:
        verify(index, get(origin + "summary.idx.sig"), keyring, "summary.idx")
        subsummaries = variant(INDEX, index).unpack()[0]
        if arch not in subsummaries:
            raise Refused(f"summary.idx has no {arch} subsummary")
        digest = bytes(subsummaries[arch][0]).hex()
        packed = get(origin + f"summaries/{digest}.gz")
        if packed is None:
            raise Refused(f"summaries/{digest}.gz, which summary.idx names, is missing")
        data = gzip.decompress(packed)
        if hashlib.sha256(data).hexdigest() != digest:
            raise Refused(f"summaries/{digest}.gz does not match the digest summary.idx signs")
        found[f"summary.idx ({arch})"] = refs(data)
    if not found:
        raise Refused(f"{origin} serves no summary: the repository is not published")
    return found


def check(path, url=URL, get=fetch):
    metadata, commit = read_bundle(path)
    origin, ref, key = metadata.get("origin"), metadata.get("ref", ""), bytes(metadata.get("gpg-keys") or b"")
    if origin != url:
        raise Refused(f"no update remote: origin is {origin!r}, not {url!r}")
    if not key:
        raise Refused("no public key (gpg-keys) for the update remote")
    with tempfile.TemporaryDirectory(prefix="cassini-keyring-") as directory:
        keyring = Path(directory) / "bundled.gpg"
        keyring.write_bytes(key)
        arch = ref.split("/")[2] if ref.count("/") == 3 else ""
        for name, published in summaries(origin, arch, keyring, get).items():
            if published.get(ref) != commit:
                raise Refused(f"{name} has {ref} at {published.get(ref)}, not the bundle's commit {commit}")
        objects = origin + f"objects/{commit[:2]}/{commit[2:]}"
        data = get(objects + ".commit")
        if data is None or hashlib.sha256(data).hexdigest() != commit:
            raise Refused(f"commit {commit} is not published at {origin}")
        verify(data, get(objects + ".commitmeta"), keyring, f"commit {commit}")
    return ref, commit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        ref, commit = check(args.bundle)
    except Refused as error:
        print(f"::error::{args.bundle}: {error} (G15: release only once D45.1 serves this signed commit)")
        return 1
    print(f"{args.bundle}: {ref} at {commit} is in {URL}, its summaries and commit signed by the bundled key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
