# SPDX-License-Identifier: GPL-3.0-or-later
"""D4 transactions against a loopback HTTP fixture; no request ever goes to dist."""

import ctypes
import hashlib
import io
import json
import shutil
import struct
import threading
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from cassini_viewer import zstd_stream
from cassini_viewer.model_download import DownloadInterrupted, InsufficientSpace, IntegrityError, ModelStore
from cassini_viewer.model_manifest import Manifest

ID, REVISION = "fixture-model", "a" * 64
NOTICE = b"Fixture model notices. These synthetic test weights are CC0.\n"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def compress(data):
    # Build two ordinary frames plus a valid no-checksum seek table. The
    # production decoder only decompresses; this independent fixture encodes.
    lib = zstd_stream.library()
    lib.ZSTD_compressBound.argtypes, lib.ZSTD_compressBound.restype = [ctypes.c_size_t], ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    frames, table = [], bytearray()
    for block in (data[: len(data) // 2], data[len(data) // 2 :]):
        buffer = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(block)))
        encoded = lib.ZSTD_compress(buffer, len(buffer), block, len(block), 1)
        assert not lib.ZSTD_isError(encoded)
        frames.append(buffer.raw[:encoded])
        table.extend(struct.pack("<II", encoded, len(block)))
    table.extend(struct.pack("<IBI", len(frames), 0, 0x8F92EAB1))
    return b"".join(frames) + struct.pack("<II", 0x184D2A5E, len(table)) + table


@pytest.fixture
def remote():
    state = SimpleNamespace(data=bytes(range(256)) * 3000, requests=[], mode="normal", notice_mode="normal")
    state.compressed = compress(state.data)
    state.etag = '"' + sha(state.compressed) + '"'
    state.counts = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.requests.append((self.path, dict(self.headers)))
            state.counts[self.path] += 1
            if self.path == "/manifest.json":
                body = json.dumps(state.manifest).encode()
                code, etag = 200, None
            elif self.path == "/NOTICE.txt":
                body, code, etag = NOTICE, 200, None
            elif self.path == "/" + state.key:
                if state.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/forbidden")
                    self.end_headers()
                    return
                body, code, etag = state.compressed, 200, state.etag
                offset = self.headers.get("Range")
                if offset and state.mode == "416":
                    state.mode = "normal"
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */" + str(len(body)))
                    self.end_headers()
                    return
                if offset and self.headers.get("If-Range") == etag:
                    start = int(offset.removeprefix("bytes=").removesuffix("-"))
                    body, code = body[start:], 206
                elif offset:
                    start = 0  # If-Range no longer matches: send a complete 200.
                else:
                    start = 0
            else:
                self.send_error(404)
                return
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            if etag:
                self.send_header("ETag", "W/" + etag if state.mode == "weak" else etag)
            if code == 206:
                reported = 0 if state.mode == "wrong-range" else start
                self.send_header(
                    "Content-Range", f"bytes {reported}-{len(state.compressed) - 1}/{len(state.compressed)}"
                )
            self.end_headers()
            truncate = (
                self.path == "/" + state.key and state.mode in ("truncate", "weak") and state.counts[self.path] == 1
            ) or (self.path == "/NOTICE.txt" and state.notice_mode == "truncate")
            try:
                self.wfile.write(body[: len(body) // 2] if truncate else body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            if truncate:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.origin = f"http://127.0.0.1:{server.server_port}"
    state.key = "models/files/" + sha(state.compressed) + "/weights.onnx.zst"
    state.manifest = dict(
        schema_version=2,
        base_url=state.origin,
        models=[
            dict(
                id=ID,
                name="Fixture model",
                description="Synthetic fixture weights",
                license="CC0-1.0",
                source_url="https://example.invalid/fixture-source",
                source_sha256=REVISION,
                files=[dict(path="weights.onnx", artifact=state.key, required=True)],
            )
        ],
        artifacts=[
            dict(
                key=state.key,
                url=state.origin + "/" + state.key,
                size=len(state.compressed),
                sha256=sha(state.compressed),
                uncompressed_size=len(state.data),
                uncompressed_sha256=sha(state.data),
                encoding="zstd-seekable",
            )
        ],
    )
    state.model = lambda: Manifest(json.dumps(state.manifest).encode(), origin=state.origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join(2)


def store(tmp_path, remote, **options):
    return ModelStore(tmp_path / "models", origin=remote.origin, fixture=True, **options)


def download(client, remote, **options):
    return client.download(ID, REVISION, manifest=remote.model(), **options)


def test_verified_seekable_install_reuse_without_network_and_remove(remote, tmp_path):
    client = store(tmp_path, remote)
    manifest = client.fetch_manifest()
    result = client.download(ID, REVISION, manifest=manifest)
    assert result == client.root / ID / REVISION
    assert (result / "weights.onnx").read_bytes() == remote.data
    assert (result / ".cassini-NOTICE.txt").read_bytes() == (client.root / "NOTICE.txt").read_bytes() == NOTICE
    assert client.installed(ID, REVISION)
    count = len(remote.requests)
    assert client.download(ID, REVISION) == result  # No manifest/HEAD/NOTICE/weight request on reuse.
    assert len(remote.requests) == count
    assert all(headers["Accept-Encoding"] == "identity" for _, headers in remote.requests)
    client.remove(ID, REVISION)
    assert not result.exists() and not client.installed(ID, REVISION)
    assert len(remote.requests) == count


def test_truncated_transfer_keeps_bytes_and_resumes_with_actual_length_and_if_range(remote, tmp_path):
    client = store(tmp_path, remote)
    remote.mode = "truncate"
    with pytest.raises(DownloadInterrupted):
        download(client, remote)
    assert not (client.root / ID / REVISION).exists()
    (partial,) = client.root.glob(".downloads/*/*/*.part")
    length = partial.stat().st_size
    assert 0 < length < len(remote.compressed)
    remote.mode = "normal"
    assert download(client, remote).is_dir()
    requests = [headers for path, headers in remote.requests if path == "/" + remote.key]
    assert requests[-1]["Range"] == f"bytes={length}-"
    assert requests[-1]["If-Range"] == remote.etag
    assert not list(client.root.rglob("*.part"))


@pytest.mark.parametrize("mode", ["416", "wrong-range", "changed-etag", "weak"])
def test_resume_never_appends_an_unauthorized_response(remote, tmp_path, mode):
    client = store(tmp_path, remote)
    remote.mode = "weak" if mode == "weak" else "truncate"
    with pytest.raises(DownloadInterrupted):
        download(client, remote)
    remote.mode = mode if mode in ("416", "wrong-range") else "normal"
    if mode == "changed-etag":
        (metadata,) = client.root.glob(".downloads/*/*/*.json")
        value = json.loads(metadata.read_text())
        value["etag"] = '"stale-etag"'
        metadata.write_text(json.dumps(value))
    result = download(client, remote)
    assert (result / "weights.onnx").read_bytes() == remote.data
    requests = [headers for path, headers in remote.requests if path == "/" + remote.key]
    if mode == "weak":
        assert "Range" not in requests[-1]
    elif mode in ("416", "wrong-range"):
        assert "Range" in requests[-2] and "Range" not in requests[-1]
    else:
        assert requests[-1]["If-Range"] == '"stale-etag"'


def test_compressed_hash_is_checked_before_decoder_and_invalid_bytes_are_discarded(remote, tmp_path, monkeypatch):
    client = store(tmp_path, remote)
    bad = "0" * 64
    artifact = remote.manifest["artifacts"][0]
    remote.key = "models/files/" + bad + "/weights.onnx.zst"
    artifact.update(key=remote.key, url=remote.origin + "/" + remote.key, sha256=bad)
    remote.manifest["models"][0]["files"][0]["artifact"] = remote.key
    monkeypatch.setattr(
        zstd_stream, "decode", lambda *_args, **_kw: pytest.fail("Decoder ran before compressed verification")
    )
    with pytest.raises(IntegrityError, match="digest"):
        download(client, remote)
    assert not client.installed(ID, REVISION) and not (client.root / ID / REVISION).exists()
    assert not list(client.root.rglob("*.part"))


def test_expanded_hash_is_required_and_no_temporary_decoded_file_is_published(remote, tmp_path):
    client = store(tmp_path, remote)
    remote.manifest["artifacts"][0]["uncompressed_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="digest"):
        download(client, remote)
    assert not (client.root / ID / REVISION).exists()
    assert not list(client.root.rglob(".decoded-*"))


def test_decoder_rejects_invalid_or_oversized_expansion():
    with pytest.raises(ValueError):
        zstd_stream.decode(io.BytesIO(b"not zstandard"), io.BytesIO(), 5)
    encoded = compress(b"valid frame" * 100)
    with pytest.raises(ValueError, match="declared size"):
        zstd_stream.decode(io.BytesIO(encoded), io.BytesIO(), 1)
    with pytest.raises(ValueError, match="Incomplete"):
        zstd_stream.decode(io.BytesIO(encoded[:-3]), io.BytesIO(), 1100)


def test_low_space_refuses_before_any_artifact_or_notice_request(remote, tmp_path):
    gib = 1024**3
    client = store(tmp_path, remote, disk_usage=lambda _: SimpleNamespace(total=100 * gib, free=2 * gib))
    model = remote.model().model(ID, REVISION)
    plan = client.plan(model)
    assert plan.reserve_bytes == 2 * gib
    assert plan.download_bytes == len(remote.compressed) and plan.installed_bytes == len(remote.data)
    with pytest.raises(InsufficientSpace) as error:
        download(client, remote)
    assert error.value.plan == plan
    assert not remote.requests
    assert not (client.root / ID / REVISION).exists()


def test_notice_interruption_preserves_verified_decoded_file_for_retry(remote, tmp_path):
    client = store(tmp_path, remote)
    remote.notice_mode = "truncate"
    with pytest.raises(DownloadInterrupted):
        download(client, remote)
    assert not (client.root / ID / REVISION).exists()
    assert remote.counts["/" + remote.key] == 1
    remote.notice_mode = "normal"
    result = download(client, remote)
    assert (result / ".cassini-NOTICE.txt").read_bytes() == NOTICE
    assert remote.counts["/" + remote.key] == 1


def test_exact_pin_is_required_and_no_latest_revision_is_followed(remote, tmp_path):
    client = store(tmp_path, remote)
    for revision in ["", "b" * 64]:
        with pytest.raises(ValueError):
            client.download(ID, revision, manifest=remote.model())
    assert not remote.requests


def test_wrong_origin_and_redirect_are_rejected(remote, tmp_path):
    with pytest.raises(ValueError):
        ModelStore(tmp_path, origin=remote.origin)
    with pytest.raises(ValueError):
        ModelStore(tmp_path, origin="https://example.invalid", fixture=True)
    remote.mode = "redirect"
    with pytest.raises(ValueError, match="origin"):
        download(store(tmp_path, remote), remote)
    assert len(remote.requests) == 1


@pytest.mark.parametrize(
    "bad",
    [".", "../escape", "/absolute", "a/../../bad", "a\\bad", ".cassini-model.json", ".cassini-NOTICE.txt", "a//b"],
)
def test_manifest_rejects_unsafe_destination_paths_before_network(remote, bad):
    remote.manifest["models"][0]["files"][0]["path"] = bad
    with pytest.raises(ValueError):
        remote.model()
    assert not remote.requests


def test_existing_revision_is_never_overwritten_and_tampering_is_detected(remote, tmp_path):
    client = store(tmp_path, remote)
    final = download(client, remote)
    (final / "weights.onnx").write_bytes(b"tampered")
    assert not client.installed(ID, REVISION)
    count = len(remote.requests)
    with pytest.raises(IntegrityError, match="Remove"):
        download(client, remote)
    assert len(remote.requests) == count
    assert (final / "weights.onnx").read_bytes() == b"tampered"


def test_two_installers_share_the_pin_lock_and_only_download_once(remote, tmp_path):
    clients = [store(tmp_path, remote), store(tmp_path, remote)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda client: download(client, remote), clients))
    assert results[0] == results[1]
    assert remote.counts["/" + remote.key] == remote.counts["/NOTICE.txt"] == 1


def test_cancellation_does_not_publish_and_the_partial_can_resume(remote, tmp_path):
    client = store(tmp_path, remote)
    event = threading.Event()

    def progress(phase, *_args):
        if phase == "downloading":
            event.set()

    with pytest.raises(CancelledError):
        download(client, remote, cancelled=event, progress=progress)
    assert not (client.root / ID / REVISION).exists()
    result = download(client, remote)
    assert (result / "weights.onnx").read_bytes() == remote.data


def test_symlinked_revision_cannot_modify_an_outside_folder(remote, tmp_path):
    client = store(tmp_path, remote)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep")
    client.root.mkdir()
    (client.root / ID).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        download(client, remote)
    assert (outside / "sentinel").read_text() == "keep"
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert not remote.requests


@pytest.mark.parametrize(
    "length", [0, 1, zstd_stream.BLOCK - 1, zstd_stream.BLOCK, zstd_stream.BLOCK + 1, zstd_stream.BLOCK * 3]
)
def test_frame_boundaries_flush_without_whole_model_reads_or_writes(length):
    payload = (bytes(range(256)) * (length // 256 + 1))[:length]
    encoded = compress(payload)

    class Source(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= zstd_stream.BLOCK
            return super().read(count)

    class Sink:
        def __init__(self):
            self.hash, self.count = hashlib.sha256(), 0

        def write(self, block):
            assert len(block) <= zstd_stream.BLOCK
            self.hash.update(block)
            self.count += len(block)

    sink = Sink()
    assert zstd_stream.decode(Source(encoded), sink, length) == length
    assert sink.count == length and sink.hash.hexdigest() == sha(payload)


def test_stage_symlink_is_rejected_before_any_model_request(remote, tmp_path):
    client = store(tmp_path, remote)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    client.root.mkdir()
    (client.root / ".downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        download(client, remote)
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_text() == "keep"
    assert not remote.requests


def test_write_failure_cannot_publish_a_partial_revision_and_retry_reuses_download(remote, tmp_path, monkeypatch):
    client = store(tmp_path, remote)
    original = zstd_stream.decode

    def disk_full(source, output, size, **kwargs):
        output.write(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(zstd_stream, "decode", disk_full)
    with pytest.raises(OSError, match="space"):
        download(client, remote)
    assert not (client.root / ID / REVISION).exists()
    assert not list(client.root.rglob(".decoded-*"))
    monkeypatch.setattr(zstd_stream, "decode", original)
    assert download(client, remote).is_dir()
    assert remote.counts["/" + remote.key] == 1


def test_unchecked_staged_files_are_never_published(remote, tmp_path):
    client = store(tmp_path, remote)
    staged = client.root / ".downloads" / ID / REVISION / "files"
    staged.mkdir(parents=True)
    (staged / "unexpected").write_bytes(b"not a manifest file")
    with pytest.raises(IntegrityError, match="Unchecked"):
        download(client, remote)
    assert not (client.root / ID / REVISION).exists()


def test_resume_space_plan_counts_only_bytes_still_needed(remote, tmp_path):
    client = store(tmp_path, remote)
    original = client.plan(remote.model().model(ID, REVISION))
    remote.notice_mode = "truncate"
    with pytest.raises(DownloadInterrupted):
        download(client, remote)
    staged = client.plan(remote.model().model(ID, REVISION))
    assert staged.remaining_download_bytes == 0
    assert staged.required_bytes < original.required_bytes
    # Enough for remaining metadata + recording reserve; not for re-downloading
    # a model that is already checked on disk. The retry must still complete.
    client.disk_usage = lambda _: SimpleNamespace(total=10 * 1024**3, free=1024**3 + staged.required_bytes)
    remote.notice_mode = "normal"
    assert download(client, remote).is_dir()
    assert remote.counts["/" + remote.key] == 1


def test_hardlinked_partial_is_not_truncated_or_allowed_to_modify_an_outside_file(remote, tmp_path):
    import os

    client = store(tmp_path, remote)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep outside data unchanged")
    staging = client.root / ".downloads" / ID / REVISION
    staging.mkdir(parents=True)
    os.link(outside, staging / (sha(remote.compressed) + ".part"))
    with pytest.raises(ValueError, match="private regular"):
        download(client, remote)
    assert outside.read_bytes() == b"keep outside data unchanged"
    assert not (client.root / ID / REVISION).exists()
    client.remove(ID, REVISION)
    assert outside.read_bytes() == b"keep outside data unchanged"


def test_original_model_notice_filename_is_preserved_separately_from_distribution_notices(remote, tmp_path):
    remote.manifest["models"][0]["files"][0]["path"] = "NOTICE.txt"
    client = store(tmp_path, remote)
    result = download(client, remote)
    assert (result / "NOTICE.txt").read_bytes() == remote.data
    assert (result / ".cassini-NOTICE.txt").read_bytes() == NOTICE
    assert (client.root / "NOTICE.txt").read_bytes() == NOTICE
    assert client.installed(ID, REVISION)


@pytest.mark.parametrize("linked_component", [0, 1, 2])
def test_remove_refuses_symlinked_partial_parent_before_deleting_any_revision(remote, tmp_path, linked_component):
    client = store(tmp_path, remote)
    installed = download(client, remote)
    shutil.rmtree(client.root / ".downloads")
    parts = (".downloads", ID, REVISION)
    anchor = client.root.joinpath(*parts[: linked_component + 1])
    anchor.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    redirected = outside.joinpath(*parts[linked_component + 1 :])
    redirected.mkdir(parents=True)
    (redirected / "keep.txt").write_bytes(b"Outside data must survive Remove")
    anchor.symlink_to(outside, target_is_directory=True)
    requests = list(remote.requests)
    with pytest.raises(ValueError, match="symbolic"):
        client.remove(ID, REVISION)
    assert (redirected / "keep.txt").read_bytes() == b"Outside data must survive Remove"
    assert installed.is_dir() and client.installed(ID, REVISION)
    assert remote.requests == requests


@pytest.mark.parametrize("component", ["root", "missing-parent"])
def test_store_rejects_a_root_or_absent_ancestor_later_replaced_by_symlink(remote, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "NOTICE.txt").write_text("Keep this notice")
    root = tmp_path / "models" if component == "root" else tmp_path / "missing" / "models"
    client = ModelStore(root, origin=remote.origin, fixture=True)
    anchor = root if component == "root" else root.parent
    anchor.symlink_to(outside, target_is_directory=True)
    for operation in (
        lambda: download(client, remote),
        lambda: client.remove(ID, REVISION),
        lambda: client.plan(remote.model().model(ID, REVISION)),
    ):
        with pytest.raises(ValueError, match="symbolic"):
            operation()
    assert not remote.requests and (outside / "NOTICE.txt").read_text() == "Keep this notice"
    assert sorted(p.name for p in outside.iterdir()) == ["NOTICE.txt"]


def test_matching_staged_weight_beneath_symlink_is_rejected_before_reuse_or_notice_write(remote, tmp_path):
    client = store(tmp_path, remote)
    remote.manifest["models"][0]["files"][0]["path"] = "sub/weights.onnx"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "weights.onnx").write_bytes(remote.data)
    files = client.root / ".downloads" / ID / REVISION / "files"
    files.mkdir(parents=True)
    (files / "sub").symlink_to(outside, target_is_directory=True)
    for operation in (lambda: download(client, remote), lambda: client.plan(remote.model().model(ID, REVISION))):
        with pytest.raises(ValueError, match="symbolic"):
            operation()
    assert not remote.requests and not (client.root / "NOTICE.txt").exists()
    assert (outside / "weights.onnx").read_bytes() == remote.data


@pytest.mark.parametrize("extra", ["regular", "symlink", "fifo"])
def test_installed_revision_rejects_unchecked_extra_files(remote, tmp_path, extra):
    import os

    client = store(tmp_path, remote)
    final = download(client, remote)
    path = final / "unchecked"
    if extra == "regular":
        path.write_bytes(b"Not in receipt")
    elif extra == "symlink":
        path.symlink_to(final / "weights.onnx")
    else:
        os.mkfifo(path)
    assert not client.installed(ID, REVISION)
    with pytest.raises(IntegrityError, match="damaged"):
        download(client, remote)


def test_supplied_manifest_pins_win_over_a_self_consistent_forged_receipt(remote, tmp_path):
    client = store(tmp_path, remote)
    final = download(client, remote)
    receipt_path = final / ".cassini-model.json"
    receipt = json.loads(receipt_path.read_text())
    replacement = b"Different self-consistent model bytes"
    (final / "weights.onnx").write_bytes(replacement)
    receipt["files"][0].update(size=len(replacement), sha256=sha(replacement))
    receipt_path.write_text(json.dumps(receipt))
    before = len(remote.requests)
    assert not client.installed(ID, REVISION, model=remote.model().model(ID, REVISION))
    with pytest.raises(IntegrityError, match="damaged"):
        download(client, remote)
    assert len(remote.requests) == before


def test_200_restart_invalidates_validator_before_truncation_can_fail(remote, tmp_path, monkeypatch):
    from cassini_viewer import model_download

    client = store(tmp_path, remote)
    remote.mode = "truncate"
    with pytest.raises(DownloadInterrupted):
        download(client, remote)
    (partial,) = client.root.glob(".downloads/*/*/*.part")
    metadata = partial.with_suffix(".json")
    value = json.loads(metadata.read_text())
    value["etag"] = '"old-etag"'
    metadata.write_text(json.dumps(value))
    old_bytes = partial.read_bytes()
    remote.mode = "normal"

    def fail(*args):
        raise OSError("Synthetic crash before truncation")

    with monkeypatch.context() as patch:
        patch.setattr(model_download.os, "ftruncate", fail)
        with pytest.raises(OSError, match="Synthetic crash"):
            download(client, remote)
    assert partial.read_bytes() == old_bytes and not metadata.exists()
    artifact = remote.model().model(ID, REVISION).files[0].artifact
    assert client._resume(artifact, partial) == (0, None)
    assert (download(client, remote) / "weights.onnx").read_bytes() == remote.data
    requests = [headers for path, headers in remote.requests if path == "/" + remote.key]
    assert "Range" not in requests[-1]


def test_abrupt_exit_decode_and_metadata_temporaries_are_cleaned_before_space_plan(remote, tmp_path):
    client = store(tmp_path, remote)
    staging = client.root / ".downloads" / ID / REVISION
    files = staging / "files"
    files.mkdir(parents=True)
    (files / (".decoded-" + "a" * 32)).write_bytes(b"abandoned decode")
    (files / ".metadata-a1b2c3d4").write_bytes(b"abandoned receipt")
    (staging / ".metadata-12345678").write_bytes(b"abandoned validator")
    original = client.plan
    checked = []

    def plan(*args, **kwargs):
        assert not list(staging.rglob(".decoded-*")) and not list(staging.rglob(".metadata-*"))
        checked.append(True)
        return original(*args, **kwargs)

    client.plan = plan
    assert (download(client, remote) / "weights.onnx").read_bytes() == remote.data
    assert checked


def test_crash_cleanup_does_not_follow_a_temporary_symlink(remote, tmp_path):
    client = store(tmp_path, remote)
    files = client.root / ".downloads" / ID / REVISION / "files"
    files.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"Keep outside data")
    (files / (".decoded-" + "a" * 32)).symlink_to(outside)
    with pytest.raises(IntegrityError, match="nonregular"):
        download(client, remote)
    assert not remote.requests and outside.read_bytes() == b"Keep outside data"
