# SPDX-License-Identifier: GPL-3.0-or-later
"""D4 schema-v2 model pins. A revision is always (id, source_sha256), never latest."""

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

ORIGIN = "https://dist.gocassini.com"
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RECEIPT = ".cassini-model.json"
NOTICE = ".cassini-NOTICE.txt"


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("Invalid model digest")
    return value


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid model identifier")
    return value


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid model file path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in (".", "..") for part in path.parts)
        or value in (".", "..", RECEIPT, NOTICE)
        or (path.parts and path.parts[0] in (RECEIPT, NOTICE))
    ):
        raise ValueError("Invalid model file path")
    return value


def origin_url(value, origin=ORIGIN):
    base, url = urlsplit(origin), urlsplit(value)
    if (
        url.scheme != base.scheme
        or url.netloc != base.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Model downloads must use their declared origin")
    return value


def size(value, *, positive=False):
    if type(value) is not int or value < int(positive) or value > 2**63 - 1:
        raise ValueError("Invalid model size")
    return value


@dataclass(frozen=True)
class Artifact:
    key: str
    url: str
    size: int
    sha256: str
    uncompressed_size: int
    uncompressed_sha256: str


@dataclass(frozen=True)
class ModelFile:
    path: str
    artifact: Artifact
    required: bool


@dataclass(frozen=True)
class Model:
    id: str
    source_sha256: str
    name: str
    license: str
    files: tuple[ModelFile, ...]
    description: str
    source_url: str

    @property
    def download_bytes(self):
        return sum(file.artifact.size for file in self.files)

    @property
    def installed_bytes(self):
        return sum(file.artifact.uncompressed_size for file in self.files)


class Manifest:
    def __init__(self, raw, *, origin=ORIGIN):
        try:
            self._parse(raw, origin)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Invalid model manifest structure") from error

    def _parse(self, raw, origin):
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Model manifest too large")
        value = json.loads(raw)
        if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
            raise ValueError("Unsupported model manifest version")
        if value.get("base_url", origin).rstrip("/") != origin:
            raise ValueError("Unexpected model manifest origin")
        self.origin = origin
        artifacts = {}
        for entry in value["artifacts"]:
            sha = digest(entry["sha256"])
            key = relative_path(entry["key"])
            url = origin_url(entry["url"], origin)
            prefix = "models/files/" + sha + "/"
            decoded = unquote(urlsplit(url).path).lstrip("/")
            if not key.startswith(prefix) or decoded != key or "/" in key[len(prefix) :] or not key.endswith(".zst"):
                raise ValueError("Unexpected immutable model artifact path")
            if entry.get("encoding") != "zstd-seekable" or key in artifacts:
                raise ValueError("Invalid model artifact encoding or duplicate")
            artifacts[key] = Artifact(
                key,
                url,
                size(entry["size"], positive=True),
                sha,
                size(entry["uncompressed_size"]),
                digest(entry["uncompressed_sha256"]),
            )
        self.models = {}
        for entry in value["models"]:
            pin = identifier(entry["id"]), digest(entry["source_sha256"])
            if pin in self.models:
                raise ValueError("Duplicate model pin")
            files, paths = [], set()
            for file in entry["files"]:
                path = relative_path(file["path"])
                if path in paths or type(file["required"]) is not bool:
                    raise ValueError("Duplicate model file or invalid required flag")
                paths.add(path)
                files.append(ModelFile(path, artifacts[file["artifact"]], file["required"]))
            if not files or not all(
                isinstance(entry[k], str) and entry[k].strip() for k in ("name", "description", "license", "source_url")
            ):
                raise ValueError("Invalid model metadata")
            self.models[pin] = Model(
                *pin, entry["name"], entry["license"], tuple(files), entry["description"], entry["source_url"]
            )

    def model(self, model_id, source_sha256):
        pin = identifier(model_id), digest(source_sha256)
        if pin not in self.models:
            raise ValueError("The pinned model revision is not in this manifest")
        return self.models[pin]
