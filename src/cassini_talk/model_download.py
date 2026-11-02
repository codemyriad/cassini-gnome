# SPDX-License-Identifier: GPL-3.0-or-later
"""D4 model acquisition. Explicit requests only; verified publication, resumable bytes."""

import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from . import zstd_stream
from .model_manifest import NOTICE, ORIGIN, RECEIPT, Manifest, digest, identifier, origin_url, relative_path

BLOCK = 128 * 1024
SMALL_LIMIT = 4 * 1024 * 1024


class IntegrityError(ValueError):
    pass


class DownloadInterrupted(OSError):
    pass


@dataclass(frozen=True)
class SpacePlan:
    download_bytes: int
    installed_bytes: int
    remaining_download_bytes: int
    required_bytes: int
    reserve_bytes: int
    free_bytes: int


class InsufficientSpace(OSError):
    def __init__(self, plan):
        self.plan = plan
        super().__init__(
            f"Model needs {plan.required_bytes} bytes plus {plan.reserve_bytes} bytes of recording reserve"
        )


def model_root():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "cassini/models"


def has_downloaded_models(root=None):
    """Receipt presence signals an atomic completed download; never hash weights on GTK."""
    root = Path(root or model_root())
    if not root.exists():
        return False
    for model in root.iterdir():
        if model.is_symlink() or not model.is_dir():
            continue
        for revision in model.iterdir():
            if revision.is_symlink() or not revision.is_dir():
                continue
            try:
                receipt = _read_json(revision / RECEIPT)
                if (receipt["id"], receipt["source_sha256"]) == (model.name, revision.name) and any(
                    (revision / relative_path(file["path"])).is_file() and not (revision / file["path"]).is_symlink()
                    for file in receipt["files"]
                ):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return False


def _cancelled(event):
    if event is not None and event.is_set():
        raise CancelledError()


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json(path, value):
    _write(path, (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > SMALL_LIMIT:
            raise ValueError("Model metadata exceeds its size limit")
        raw = stream.read(SMALL_LIMIT + 1)
    if len(raw) > SMALL_LIMIT:
        raise ValueError("Model metadata exceeds its size limit")
    return json.loads(raw)


def _verify(path, expected_size, expected_sha, event=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise IntegrityError("Model file size differs: " + path.name)
        result = hashlib.sha256()
        while block := stream.read(BLOCK):
            _cancelled(event)
            result.update(block)
        after = os.fstat(stream.fileno())
    if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or result.hexdigest() != expected_sha:
        raise IntegrityError("Model file digest differs: " + path.name)


def _publish(stage, final):
    # Linux native application: an existing revision, including an empty one,
    # must never be replaced. The final complete folder appears in one rename.
    function = ctypes.CDLL(None, use_errno=True).renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(stage), -100, os.fsencode(final), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(final))
    _sync_directory(final.parent)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, newurl):
        try:
            origin_url(newurl, self.origin)
        except ValueError:
            response.close()
            raise
        return super().redirect_request(request, response, code, message, headers, newurl)


PACKAGING = ("flatpak", "deb", "rpm", "arch", "source")


def installed_packaging(config=None):
    """How this copy was built (meson -Dpackaging); a source run has no paths.json."""
    try:
        value = json.loads(Path(config or Path(__file__).with_name("paths.json")).read_text()).get("packaging")
    except (OSError, ValueError, AttributeError):
        return "source"
    return value if value in PACKAGING else "source"


def user_agent(version, packaging):
    """D54: one app-wide User-Agent for every model request; no person or device identifiers."""
    if not isinstance(version, str) or not version or packaging not in PACKAGING:
        raise ValueError("Invalid model download identity")
    return f"CassiniTalk/{version} (GNOME; Linux; {packaging})"


class ModelStore:
    def __init__(
        self, root=None, *, user_agent, origin=ORIGIN, fixture=False, disk_usage=shutil.disk_usage, timeout=10
    ):
        if not isinstance(user_agent, str) or not user_agent.startswith("CassiniTalk/") or len(user_agent) > 200:
            raise ValueError("Model requests need the app's CassiniTalk User-Agent (D54)")
        self.user_agent = user_agent
        origin = origin.rstrip("/")
        if origin != ORIGIN:
            parsed = urlsplit(origin)
            if not fixture or parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or parsed.path:
                raise ValueError("Only dist.gocassini.com or an explicit loopback test fixture is allowed")
        self.root = Path(root or model_root()).resolve()
        self.origin = origin
        self.disk_usage = disk_usage
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_Redirect(origin))

    def _check_directories(self, path):
        # root was canonicalized at construction, but a previously missing
        # component (or root itself) can subsequently become a symlink.
        path.relative_to(self.root)
        for directory in (*reversed(path.parents), path):
            if directory.is_symlink():
                raise ValueError("Model storage directories must not be symbolic links")
            if directory.exists() and not directory.is_dir():
                raise ValueError("Model storage path is not a directory")

    def _mkdir(self, path):
        self._check_directories(path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Model storage directories must not be symbolic links")
            current.mkdir(exist_ok=True, mode=0o700)

    def _directory(self, model_id, revision):
        path = self.root / identifier(model_id) / digest(revision)
        self._check_directories(path)
        return path

    @contextmanager
    def _lock(self, model_id, revision, event):
        locks = self.root / ".locks"
        self._mkdir(locks)
        path = locks / (identifier(model_id) + "-" + digest(revision))
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 30
            while True:
                _cancelled(event)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("This model revision is busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def _open(self, url, **headers):
        origin_url(url, self.origin)
        request = urllib.request.Request(
            url, headers={"Accept-Encoding": "identity", "User-Agent": self.user_agent, **headers}
        )
        response = self.opener.open(request, timeout=self.timeout)
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            response.close()
            raise ValueError("Model response must use identity Content-Encoding")
        return response

    def _small(self, name, event):
        _cancelled(event)
        with self._open(self.origin + "/" + name) as response:
            if response.status != 200:
                raise ValueError("Unexpected model metadata status")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > SMALL_LIMIT):
                raise ValueError("Invalid model metadata length")
            result = bytearray()
            while True:
                _cancelled(event)
                block = response.read1(BLOCK)
                if not block:
                    break
                result.extend(block)
                if len(result) > SMALL_LIMIT:
                    raise ValueError("Model metadata exceeds its size limit")
            if length is not None and len(result) != int(length):
                raise DownloadInterrupted("Model metadata transfer interrupted")
            return bytes(result)

    def fetch_manifest(self, *, cancelled=None):
        return Manifest(self._small("manifest.json", cancelled), origin=self.origin)

    def plan(self, model, *, cancelled=None):
        self._check_directories(self.root)
        directory = self.root
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        usage = self.disk_usage(directory)
        reserve = max(1024**3, usage.total // 50)
        remaining, expanded = 0, 0
        if not self.installed(model.id, model.source_sha256, model=model, cancelled=cancelled):
            staging = self.root / ".downloads" / model.id / model.source_sha256
            for file in model.files:
                artifact = file.artifact
                self._check_directories((staging / "files" / file.path).parent)
                try:
                    _verify(
                        staging / "files" / file.path,
                        artifact.uncompressed_size,
                        artifact.uncompressed_sha256,
                        cancelled,
                    )
                except (OSError, IntegrityError):
                    offset, _etag = self._resume(artifact, staging / (artifact.sha256 + ".part"))
                    remaining += artifact.size - offset
                    expanded += artifact.uncompressed_size
        # Existing bytes already reduced free space. Reserve only new staging
        # bytes, including both bounded notice copies, so a retry can finish.
        needed = remaining + expanded + 2 * SMALL_LIMIT
        return SpacePlan(model.download_bytes, model.installed_bytes, remaining, needed, reserve, usage.free)

    def installed(self, model_id, revision, *, model=None, expected_files=None, cancelled=None):
        final = self._directory(model_id, revision)
        try:
            receipt = _read_json(final / RECEIPT)
            if (receipt["id"], receipt["source_sha256"]) != (model_id, revision) or not receipt["files"]:
                return False
            records = receipt["files"]
            paths = {relative_path(file["path"]) for file in records}
            if len(paths) != len(records) or self._tree_paths(final) != paths | {NOTICE, RECEIPT}:
                return False
            if model is not None and sorted(records, key=lambda f: f["path"]) != sorted(
                [
                    dict(path=f.path, size=f.artifact.uncompressed_size, sha256=f.artifact.uncompressed_sha256)
                    for f in model.files
                ],
                key=lambda f: f["path"],
            ):
                return False
            if (
                expected_files is not None
                and {file["path"]: (file["size"], file["sha256"]) for file in records} != expected_files
            ):
                return False
            for file in receipt["files"]:
                path = relative_path(file["path"])
                if any(
                    parent.is_symlink()
                    for parent in (final / path).parents
                    if parent != final and final in parent.parents
                ):
                    return False
                _verify(final / path, file["size"], digest(file["sha256"]), cancelled)
            _verify(final / NOTICE, receipt["notice_size"], digest(receipt["notice_sha256"]), cancelled)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def download(self, model_id, revision, *, manifest=None, cancelled=None, progress=lambda *_: None):
        final = self._directory(model_id, revision)
        if manifest is not None and manifest.origin != self.origin:
            raise ValueError("Manifest and downloader origins differ")
        model = manifest.model(model_id, revision) if manifest is not None else None
        with self._lock(model_id, revision, cancelled):
            if self.installed(model_id, revision, model=model, cancelled=cancelled):
                return final
            if final.exists():
                raise IntegrityError("Remove the damaged model revision before downloading it again")
            manifest = manifest or self.fetch_manifest(cancelled=cancelled)
            model = manifest.model(model_id, revision)
            if manifest.origin != self.origin:
                raise ValueError("Manifest and downloader origins differ")
            staging = self.root / ".downloads" / model_id / revision
            # Abrupt app exit may interrupt decode or an atomic metadata write.
            # Only our named regular temporaries are disposable; retained .part
            # files and their validators still resume under the normal checks.
            self._clean_temporaries(staging)
            plan = self.plan(model, cancelled=cancelled)
            if plan.free_bytes - plan.required_bytes < plan.reserve_bytes:
                raise InsufficientSpace(plan)
            zstd_stream.library()  # Fail before requesting model bytes if unavailable.
            staging = self.root / ".downloads" / model_id / revision
            files = staging / "files"
            self._mkdir(files)
            records = []
            for file in model.files:
                _cancelled(cancelled)
                target = files / file.path
                artifact = file.artifact
                self._check_directories(target.parent)
                try:
                    _verify(target, artifact.uncompressed_size, artifact.uncompressed_sha256, cancelled)
                except (OSError, IntegrityError):
                    self._mkdir(target.parent)
                    compressed = staging / (artifact.sha256 + ".part")
                    self._download_file(artifact, compressed, cancelled, progress)
                    try:
                        progress("verifying", file.path, artifact.size, artifact.size)
                        _verify(compressed, artifact.size, artifact.sha256, cancelled)
                    except IntegrityError:
                        compressed.unlink(missing_ok=True)
                        compressed.with_suffix(".json").unlink(missing_ok=True)
                        raise
                    temporary = target.with_name(".decoded-" + uuid4().hex)
                    try:
                        progress("unpacking", file.path, 0, artifact.uncompressed_size)
                        with compressed.open("rb") as source, temporary.open("xb") as output:
                            temporary.chmod(0o600)
                            zstd_stream.decode(source, output, artifact.uncompressed_size, cancelled=cancelled)
                            output.flush()
                            os.fsync(output.fileno())
                        _verify(temporary, artifact.uncompressed_size, artifact.uncompressed_sha256, cancelled)
                        os.replace(temporary, target)
                        _sync_directory(target.parent)
                    finally:
                        temporary.unlink(missing_ok=True)
                    compressed.unlink(missing_ok=True)
                    compressed.with_suffix(".json").unlink(missing_ok=True)
                else:
                    # A previously verified expanded file still contributes to
                    # resumed download progress, without fetching it again.
                    progress("verifying", file.path, artifact.size, artifact.size)
                records.append(
                    dict(path=file.path, size=artifact.uncompressed_size, sha256=artifact.uncompressed_sha256)
                )
            notice = self._small("NOTICE.txt", cancelled)
            if not notice.decode("utf-8").strip():
                raise ValueError("Distribution notices are empty")
            _write(files / NOTICE, notice)
            _json(
                files / RECEIPT,
                dict(
                    id=model.id,
                    source_sha256=revision,
                    name=model.name,
                    license=model.license,
                    files=records,
                    notice_size=len(notice),
                    notice_sha256=hashlib.sha256(notice).hexdigest(),
                ),
            )
            _cancelled(cancelled)
            self._mkdir(final.parent)
            expected_paths = {record["path"] for record in records} | {NOTICE, RECEIPT}
            if self._tree_paths(files) != expected_paths:
                raise IntegrityError("Unchecked files in staged model")
            # About links to a local copy, never a network request on opening it.
            self._check_directories(self.root)
            _write(self.root / "NOTICE.txt", notice)
            _cancelled(cancelled)
            _publish(files, final)
            shutil.rmtree(staging)
            progress("installed", model.id, model.download_bytes, model.download_bytes)
            return final

    def _clean_temporaries(self, staging):
        for relative in self._tree_paths(staging):
            path = staging / relative
            if re.fullmatch(r"(?:\.decoded-[a-f0-9]{32}|\.metadata-[a-z0-9_]{8})", path.name):
                path.unlink()

    def _tree_paths(self, root):
        self._check_directories(root)
        paths = set()
        for directory, folders, names in os.walk(root, followlinks=False):
            for name in folders + names:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise IntegrityError("Unchecked nonregular path or symbolic link in model")
            paths.update(str((Path(directory) / name).relative_to(root)) for name in names)
        return paths

    def _resume(self, artifact, partial):
        self._check_directories(partial.parent)
        try:
            info = partial.lstat()
            old = _read_json(partial.with_suffix(".json"))
            if not isinstance(old, dict):
                return 0, None
            etag = old.get("etag")
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_size <= artifact.size
                and old["url"] == artifact.url
                and old["sha256"] == artifact.sha256
                and isinstance(etag, str)
                and re.fullmatch(r'"[^"\r\n]+"', etag)
            ):
                return info.st_size, etag
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return 0, None

    def _download_file(self, artifact, partial, event, progress):
        metadata = partial.with_suffix(".json")
        for _attempt in range(2):
            _cancelled(event)
            offset, etag = self._resume(artifact, partial)
            if offset == artifact.size:
                return
            if offset > artifact.size:
                offset = 0
            headers = {"Range": f"bytes={offset}-", "If-Range": etag} if offset else {}
            try:
                response = self._open(artifact.url, **headers)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 416 or not offset:
                    raise
                partial.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                continue
            with response:
                if response.status == 206:
                    expected = f"bytes {offset}-{artifact.size - 1}/{artifact.size}"
                    if (
                        not offset
                        or response.headers.get("Content-Range") != expected
                        or response.headers.get("ETag") != etag
                    ):
                        partial.unlink(missing_ok=True)
                        metadata.unlink(missing_ok=True)
                        continue
                elif response.status == 200:
                    offset = 0  # If-Range changed, or server ignored Range: replace, never append.
                else:
                    raise ValueError("Unexpected model response status")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != artifact.size - offset):
                    raise ValueError("Unexpected model response length")
                self._check_directories(partial.parent)
                fd = os.open(
                    partial,
                    os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_APPEND if offset else 0),
                    0o600,
                )
                with os.fdopen(fd, "wb") as output:
                    info = os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Model temporary files must be private regular files")
                    if not offset:
                        # Never pair the old prefix with a new validator. A
                        # crash before truncation retains the old pair; after
                        # this removal a retry starts from zero.
                        metadata.unlink(missing_ok=True)
                        _sync_directory(metadata.parent)
                        os.ftruncate(output.fileno(), 0)
                        os.fsync(output.fileno())
                    _json(metadata, dict(url=artifact.url, sha256=artifact.sha256, etag=response.headers.get("ETag")))
                    count = offset
                    try:
                        while True:
                            _cancelled(event)
                            block = response.read1(BLOCK)
                            if not block:
                                break
                            count += len(block)
                            if count > artifact.size:
                                raise IntegrityError("Compressed model exceeds its declared size")
                            output.write(block)
                            progress("downloading", artifact.key, count, artifact.size)
                    finally:
                        output.flush()
                        os.fsync(output.fileno())
                    if count != artifact.size:
                        raise DownloadInterrupted("Model transfer interrupted; verified resume is available")
            return
        raise DownloadInterrupted("The server could not resume the immutable model file safely")

    def remove(self, model_id, revision, *, cancelled=None):
        final = self._directory(model_id, revision)
        with self._lock(model_id, revision, cancelled):
            _cancelled(cancelled)
            partial = self.root / ".downloads" / model_id / revision
            # Check every cleanup ancestor before removing the installed copy.
            # rmtree refuses a final symlink, but follows symlinked parents.
            for directory in (partial, *partial.parents):
                if directory == self.root:
                    break
                if directory.is_symlink():
                    raise ValueError("Model storage directories must not be symbolic links")
            if final.exists():
                tombstone = final.with_name(".removing-" + uuid4().hex)
                os.rename(final, tombstone)
                _sync_directory(final.parent)
                shutil.rmtree(tombstone)
            if partial.exists():
                shutil.rmtree(partial)
