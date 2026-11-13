# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for vendored W0.8 pr-shots.py, GNOME manifest diffing, and CI wiring."""

import hashlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import types
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "build-aux/ci/pr-shots.py"
CANONICAL_COMMIT = "5fc25fa49b86e38d3876a0a26fcb77506bd23305"
CANONICAL_SHA256 = "d4898061024ba464e88c66dc12563d1a61aaa5d6675ccc22e3af62aa2314c994"
HAS_REAL_PIL = importlib.util.find_spec("PIL") is not None


def _install_stdlib_pil_fallback() -> None:
    """Provide the minimal RGBA/L PNG subset pr-shots.py uses when Pillow is absent."""

    class _Img:
        def __init__(self, mode: str, size: tuple[int, int], pixels: list):
            self.mode, self.size = mode, size
            self.width, self.height = size
            self.px = pixels

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def verify(self):
            return None

        def close(self):
            return None

        def putpixel(self, xy: tuple[int, int], value):
            self.px[xy[1] * self.width + xy[0]] = value

        def convert(self, mode: str):
            if mode == self.mode:
                return _Img(mode, self.size, list(self.px))
            if mode == "RGBA":
                if self.mode == "RGB":
                    return _Img("RGBA", self.size, [(r, g, b, 255) for r, g, b in self.px])
                return _Img("RGBA", self.size, [(v, v, v, 255) for v in self.px])
            if mode == "RGBa":
                rgba = self.convert("RGBA").px
                return _Img("RGBa", self.size, [(r * a // 255, g * a // 255, b * a // 255, a) for r, g, b, a in rgba])
            if mode == "RGB":
                if self.mode == "L":
                    return _Img("RGB", self.size, [(v, v, v) for v in self.px])
                return _Img("RGB", self.size, [(r, g, b) for r, g, b, *_ in self.px])
            if mode == "L":
                rgb = self.convert("RGB").px
                return _Img("L", self.size, [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in rgb])
            raise ValueError(mode)

        def split(self):
            channels = len(self.px[0])
            return tuple(_Img("L", self.size, [p[c] for p in self.px]) for c in range(channels))

        def point(self, fn):
            return _Img("L", self.size, [fn(v) for v in self.px])

        def paste(self, src, box=None, mask=None):
            if isinstance(src, _Img):
                ox, oy = box or (0, 0)
                for y in range(src.height):
                    for x in range(src.width):
                        self.px[(oy + y) * self.width + (ox + x)] = src.px[y * src.width + x]
            elif mask is not None:
                for i, m in enumerate(mask.px):
                    if m:
                        self.px[i] = src
            else:
                x0, y0, x1, y1 = box
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        self.px[y * self.width + x] = src

        def histogram(self):
            counts = [0] * 256
            for v in self.px:
                counts[v] += 1
            return counts

        def getbbox(self):
            xs = [i % self.width for i, v in enumerate(self.px) if v]
            ys = [i // self.width for i, v in enumerate(self.px) if v]
            return (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if xs else None

        def save(self, target, format: str = "PNG"):
            rgba = self.convert("RGBA")

            def chunk(tag: bytes, payload: bytes) -> bytes:
                return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

            rows = bytearray()
            for y in range(rgba.height):
                rows.append(0)
                for x in range(rgba.width):
                    rows.extend(rgba.px[y * rgba.width + x])
            ihdr = struct.pack(">IIBBBBB", rgba.width, rgba.height, 8, 6, 0, 0, 0)
            raw = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(rows)))
                + chunk(b"IEND", b"")
            )
            if hasattr(target, "write"):
                target.write(raw)
            else:
                Path(target).write_bytes(raw)

    class _ImageMod:
        MAX_IMAGE_PIXELS = 12_000_000

        @staticmethod
        def new(mode: str, size: tuple[int, int], color=0):
            if color == "white":
                fill = 255 if mode == "L" else ((255, 255, 255) if mode == "RGB" else (255, 255, 255, 255))
            elif isinstance(color, int) and mode != "L":
                fill = (color, color, color) if mode == "RGB" else (color, color, color, color)
            else:
                fill = color
            return _Img(mode, size, [fill] * (size[0] * size[1]))

        @staticmethod
        def merge(mode: str, bands):
            return _Img(mode, bands[0].size, list(zip(*(b.px for b in bands), strict=True)))

        @staticmethod
        def open(source):
            data = source.read() if hasattr(source, "read") else Path(source).read_bytes()
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Invalid PNG")
            pos, w, h, ctype, idat = 8, 0, 0, 0, bytearray()
            while pos < len(data):
                ln, tag = struct.unpack(">I4s", data[pos : pos + 8])
                payload = data[pos + 8 : pos + 8 + ln]
                pos += 12 + ln
                if tag == b"IHDR":
                    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", payload)
                    assert depth == 8 and ctype in (2, 6) and interlace == 0
                elif tag == b"IDAT":
                    idat.extend(payload)
            bpp = 4 if ctype == 6 else 3
            stride, raw = w * bpp, zlib.decompress(bytes(idat))
            prev, cur, off, px = bytearray(stride), bytearray(stride), 0, []
            for _ in range(h):
                flt, line = raw[off], raw[off + 1 : off + 1 + stride]
                off += 1 + stride
                for i in range(stride):
                    a = cur[i - bpp] if i >= bpp else 0
                    b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = (0, a, b, (a + b) >> 1, a if pa <= pb and pa <= pc else (b if pb <= pc else c))[flt]
                    cur[i] = (line[i] + pred) & 0xFF
                prev[:] = cur
                for x in range(w):
                    s = cur[x * bpp : (x + 1) * bpp]
                    px.append((s[0], s[1], s[2], s[3] if bpp == 4 else 255))
            return _Img("RGBA", (w, h), px)

    class _Chops:
        @staticmethod
        def difference(a: _Img, b: _Img):
            if a.mode == "L":
                return _Img("L", a.size, [abs(x - y) for x, y in zip(a.px, b.px, strict=True)])
            return _Img(
                a.mode,
                a.size,
                [tuple(abs(c1 - c2) for c1, c2 in zip(p1, p2, strict=True)) for p1, p2 in zip(a.px, b.px, strict=True)],
            )

        @staticmethod
        def lighter(a: _Img, b: _Img):
            return _Img("L", a.size, [max(x, y) for x, y in zip(a.px, b.px, strict=True)])

    class _Ops:
        @staticmethod
        def grayscale(a: _Img):
            return a.convert("L")

    pil = types.ModuleType("PIL")
    pil.Image, pil.ImageChops, pil.ImageOps = _ImageMod, _Chops, _Ops
    sys.modules["PIL"] = pil


def _load_modules():
    if not HAS_REAL_PIL and "PIL" not in sys.modules:
        _install_stdlib_pil_fallback()
    pr_spec = importlib.util.spec_from_file_location("pr_shots", SCRIPT)
    pr_mod = importlib.util.module_from_spec(pr_spec)
    pr_spec.loader.exec_module(pr_mod)
    man_spec = importlib.util.spec_from_file_location("ux_shots_manifest", ROOT / "build-aux/ci/ux_shots_manifest.py")
    man_mod = importlib.util.module_from_spec(man_spec)
    man_spec.loader.exec_module(man_mod)
    return pr_mod, man_mod


def _png_bytes(rgba: bytes, width: int = 8, height: int = 8, level: int = 6) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress((b"\x00" + rgba * width) * height, level)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_vendored_script_matches_canonical_cassini_ux_sha256():
    raw_lines = SCRIPT.read_bytes().splitlines(keepends=True)
    assert raw_lines[0] == b"#!/usr/bin/env python3\n"
    assert f"codemyriad/cassini-ux@{CANONICAL_COMMIT}:tools/pr-shots/pr-shots.py".encode() in raw_lines[1]
    canonical = raw_lines[0] + b"".join(raw_lines[2:])
    assert hashlib.sha256(canonical).hexdigest() == CANONICAL_SHA256


def test_pr_shots_self_test_without_polluting_step_summary(tmp_path, monkeypatch):
    pr_shots, _ = _load_modules()
    sentinel = tmp_path / "real-step-summary.md"
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    pr_shots.self_test()
    if HAS_REAL_PIL:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--self-test"], env=env, check=True, capture_output=True, text=True
        )
        assert "pr-shots.py self-test passed" in proc.stdout
    assert not sentinel.exists()


def test_gnome_manifest_diff_emits_both_modes_ignores_metadata_bytes_and_writes_step_summary(tmp_path, monkeypatch):
    pr_shots, manifest_mod = _load_modules()
    before_dir, after_dir, out_dir = tmp_path / "before", tmp_path / "after", tmp_path / "out"
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("GITHUB_RUN_ID", "43138305541")

    light_px, dark_px = b"\xf8\xf8\xfa\xff", b"\x20\x22\x28\xff"
    for root, level in ((before_dir, 1), (after_dir, 9)):
        for rel, px in (
            ("library.png", light_px),
            ("dark/library.png", dark_px),
            ("search.png", light_px),
            ("dark/search.png", dark_px),
            ("first-run-fr.png", light_px),
            ("dark/first-run-fr.png", dark_px),
        ):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # Different zlib compression levels produce different file bytes with identical RGBA pixels.
            target.write_bytes(_png_bytes(px, level=level))

    # Alter search.png (light) while keeping dark/search.png untouched; both modes must be emitted.
    (after_dir / "search.png").write_bytes(_png_bytes(b"\xe2\xf0\xf4\xff"))
    manifest_mod.write_manifest(before_dir)
    manifest_mod.write_manifest(after_dir)

    args = pr_shots.parser().parse_args(
        [
            "diff", "--layout", "gnome", "--repo", "codemyriad/cassini-gnome", "--pr", "386",
            "--base-sha", "a" * 40, "--head-sha", "b" * 40,
            "--head-runner", "self-hosted,cm-hetzner", "--base-runner", "self-hosted,cm-hetzner",
            "--before-dir", str(before_dir), "--after-dir", str(after_dir), "--out", str(out_dir),
        ]
    )  # fmt: skip
    data = pr_shots.diff(args)
    by_key = {(r["screen"], r["scheme"], r["lang"]): r for r in data["rows"]}
    assert by_key[("library", "light", "en")]["status"] == "unchanged"
    assert by_key[("library", "light", "en")]["before"] is None
    assert by_key[("search", "light", "en")]["status"] == "changed"
    assert by_key[("search", "dark", "en")]["status"] == "unchanged"
    assert (out_dir / "before/search.dark.png").is_file() and (out_dir / "after/search.dark.png").is_file()
    assert not (out_dir / "after/library.light.png").exists()
    summary = summary_file.read_text()
    assert "1 screens changed, 2 unchanged." in summary and "<img " not in summary
    assert "/pr-shots/" not in summary
    assert "`before/search.light.png`" in summary and "`after/search.light.png`" in summary
    assert (
        "Download images: [pr-shots artifact (ZIP)](https://github.com/codemyriad/cassini-gnome/actions/runs/43138305541#artifacts)."
        in summary
    )
    assert (
        '<img src="https://github.com/codemyriad/cassini-gnome/raw/pr-shots/pr-386/'
        in (out_dir / "block.md").read_text()
    )


def test_full_gnome_gallery_diff_artifact_validates_for_publication(tmp_path, monkeypatch, capsys):
    pr_shots, manifest_mod = _load_modules()
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    before_dir, after_dir, out_dir = tmp_path / "before", tmp_path / "after", tmp_path / "out"
    (before_dir / "dark").mkdir(parents=True)
    (after_dir / "dark").mkdir(parents=True)

    # Full GNOME gallery (76 screen pairs = 152 PNGs): 44 changed pairs + 32 unchanged pairs emits 264 PNGs (44 * 6),
    # matching live CI run 43138305541 while keeping the rendered PR block well within GitHub's 65 KB body limit.
    before_light = _png_bytes(b"\xf8\xf8\xfa\xff", width=4, height=4)
    before_dark = _png_bytes(b"\x20\x22\x28\xff", width=4, height=4)
    after_light = _png_bytes(b"\xe2\xf0\xf4\xff", width=4, height=4)
    after_dark = _png_bytes(b"\x28\x30\x3c\xff", width=4, height=4)
    for idx in range(76):
        name = "library.png" if idx == 0 else f"spectrogram-{idx:02d}.png"
        (before_dir / name).write_bytes(before_light)
        (before_dir / "dark" / name).write_bytes(before_dark)
        changed = idx < 44
        (after_dir / name).write_bytes(after_light if changed else before_light)
        (after_dir / "dark" / name).write_bytes(after_dark if changed else before_dark)
    manifest_mod.write_manifest(before_dir)
    manifest_mod.write_manifest(after_dir)

    head_sha = "b" * 40
    diff_args = pr_shots.parser().parse_args(
        [
            "diff", "--layout", "gnome", "--repo", "codemyriad/cassini-gnome", "--pr", "386",
            "--base-sha", "a" * 40, "--head-sha", head_sha,
            "--head-runner", "self-hosted,cm-hetzner", "--base-runner", "self-hosted,cm-hetzner",
            "--before-dir", str(before_dir), "--after-dir", str(after_dir), "--out", str(out_dir),
        ]
    )  # fmt: skip
    pr_shots.diff(diff_args)

    monkeypatch.setattr(pr_shots, "github", lambda _route: {"head": {"sha": head_sha, "ref": "feat/w08"}, "body": ""})
    pub_args = pr_shots.parser().parse_args(
        ["publish", "--repo", "codemyriad/cassini-gnome", "--pr", "386", "--dir", str(out_dir), "--dry-run"]
    )
    pr_shots.publish(pub_args)
    rendered = capsys.readouterr().out
    assert "44 screens changed, 32 unchanged." in rendered
    assert "<details><summary>32 more screens</summary>" in rendered
    assert len(rendered.encode("utf-8")) < 65_000

    files = pr_shots.directory(out_dir, require_images=False)
    files["changes.json"] = (out_dir / "changes.json").read_bytes()
    data = json.loads(files["changes.json"])
    needed = pr_shots.validated(data, files, "codemyriad/cassini-gnome", 386, head_sha)
    assert len(needed) == 264

    # Even if every single one of GNOME's 76 screen pairs changes (456 PNGs), validated() accepts it cleanly,
    # while still enforcing MAX_PUBLICATION_PNGS above the bounded cap.
    all_out = tmp_path / "out-all"
    for idx in range(44, 76):
        name = f"spectrogram-{idx:02d}.png"
        (after_dir / name).write_bytes(after_light)
        (after_dir / "dark" / name).write_bytes(after_dark)
    manifest_mod.write_manifest(after_dir)
    all_args = pr_shots.parser().parse_args(
        [
            "diff", "--layout", "gnome", "--repo", "codemyriad/cassini-gnome", "--pr", "386",
            "--base-sha", "a" * 40, "--head-sha", head_sha,
            "--head-runner", "self-hosted,cm-hetzner", "--base-runner", "self-hosted,cm-hetzner",
            "--before-dir", str(before_dir), "--after-dir", str(after_dir), "--out", str(all_out),
        ]
    )  # fmt: skip
    pr_shots.diff(all_args)
    all_files = pr_shots.directory(all_out, require_images=False)
    all_files["changes.json"] = (all_out / "changes.json").read_bytes()
    all_data = json.loads(all_files["changes.json"])
    assert len(pr_shots.validated(all_data, all_files, "codemyriad/cassini-gnome", 386, head_sha)) == 456

    # 76 changed pairs (> 40,000 B with <details>) automatically compact <details> in block(),
    # while 44 changed pairs (< 40,000 B alone) also compact when combined with a large PR body,
    # and unbalanced markers or > 65,000 B prose fail in dry-run and before checkout_host/push_host.
    all_rendered = pr_shots.block(all_data)
    assert "<details>" not in all_rendered
    assert "Showing the first 12 of 76 changed screens." in all_rendered

    capsys.readouterr()
    monkeypatch.setattr(
        pr_shots, "github", lambda _route: {"head": {"sha": head_sha, "ref": "feat/w08"}, "body": "p" * 45_000}
    )
    pr_shots.publish(pub_args)
    compacted = capsys.readouterr().out
    assert "<details>" not in compacted
    assert "Showing the first 12 of 44 changed screens." in compacted

    real_args = pr_shots.parser().parse_args(
        ["publish", "--repo", "codemyriad/cassini-gnome", "--pr", "386", "--dir", str(out_dir)]
    )
    calls = []
    monkeypatch.setattr(pr_shots, "checkout_host", lambda *_a, **_k: calls.append("checkout"))
    monkeypatch.setattr(pr_shots, "push_host", lambda *_a, **_k: calls.append("push"))
    monkeypatch.setattr(pr_shots, "command", lambda *_a, **_k: calls.append("command"))
    for bad_body, pattern in (
        (pr_shots.START + " broken", "Unbalanced screenshot markers"),
        ("x" * 64_500, "PR body exceeds GitHub limit"),
    ):
        monkeypatch.setattr(pr_shots, "github", lambda _route, b=bad_body: {"head": {"sha": head_sha}, "body": b})
        for cli_args in (pub_args, real_args):
            with pytest.raises(ValueError, match=pattern):
                pr_shots.publish(cli_args)
            assert calls == []

    monkeypatch.setattr(pr_shots, "MAX_PUBLICATION_PNGS", 455)
    with pytest.raises(ValueError, match="Publisher limit: 455 PNG files"):
        pr_shots.validated(all_data, all_files, "codemyriad/cassini-gnome", 386, head_sha)


def test_inline_slot_priority_and_variant_qualifiers():
    pr_shots, _ = _load_modules()
    base_sha, head_sha = "a" * 40, "b" * 40
    rows = []

    def add_pair(screen, *, status="changed", lang="en", device="desktop"):
        for scheme in ("light", "dark"):
            row = {
                "screen": screen,
                "label": screen.replace("-", " ").capitalize(),
                "scheme": scheme,
                "lang": lang,
                "device": device,
                "status": status,
                "changed_pct": None if status == "added" else 1.5,
            }
            k = pr_shots.key(row)
            row["key"] = k
            row["before"] = None if status == "added" else f"before/{k}.png"
            row["after"] = f"after/{k}.png"
            row["diff"] = None if status == "added" else f"diff/{k}.png"
            rows.append(row)

    for flow in (
        "background",
        "batch-import-gui",
        "companion-setup-connected",
        "companion-setup-failed",
        "companion-setup-local",
        "companion-setup-open-failed",
    ):
        add_pair(flow)
    add_pair("first-run", lang="fr")
    add_pair("first-run", lang="pt-br")
    add_pair("split-landscape", device="ipad")
    for core_screen in (
        "library",
        "recording",
        "meeting",
        "search",
        "first-run",
        "received",
        "export",
        "settings-recording",
        "settings-transcription",
        "speakers",
        "saved-voices",
    ):
        add_pair(core_screen)
    add_pair("w2-badges", status="added")

    data = {
        "version": 1,
        "app": "gnome",
        "repo": "codemyriad/cassini-gnome",
        "pr": 403,
        "base": {"sha": base_sha, "run_id": "1", "runner": "self-hosted", "image": "codemyriad"},
        "head": {"sha": head_sha, "run_id": "2", "runner": "self-hosted", "image": "codemyriad"},
        "rows": rows,
    }
    rendered = pr_shots.block(data)
    inline, details = rendered.split("<details>", 1)
    inline_labels = [line.split("|")[1].strip() for line in inline.splitlines() if line.startswith("| ")][1:]
    expected_screens = [
        "Library",
        "Recording",
        "Meeting",
        "Search",
        "W2 badges",
        "First run",
        "Received",
        "Speakers",
        "Export",
        "Settings recording",
        "Settings transcription",
        "Saved voices",
    ]
    assert inline_labels == [f"{name} · {scheme}" for name in expected_screens for scheme in ("light", "dark")]
    assert "- First run · light (fr):" in details
    assert "- First run · dark (pt-br):" in details
    assert "- Split landscape · light (ipad):" in details
    assert "- First run · light:" not in details


def test_baseline_lookup_resolves_merge_parent_and_handles_missing_baseline(tmp_path, monkeypatch):
    pr_shots, manifest_mod = _load_modules()
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    base_dir, after_dir = tmp_path / "base", tmp_path / "after"
    for root in (base_dir, after_dir):
        (root / "dark").mkdir(parents=True)
        (root / "library.png").write_bytes(_png_bytes(b"\xf8\xf8\xfa\xff"))
        (root / "dark/library.png").write_bytes(_png_bytes(b"\x20\x22\x28\xff"))
        manifest_mod.write_manifest(root)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(base_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(base_dir).as_posix())
    zip_bytes = buf.getvalue()
    digest = "sha256:" + hashlib.sha256(zip_bytes).hexdigest()

    merge_sha, parent_sha, stale_base, head_sha = "c" * 40, "1" * 40, "0" * 40, "2" * 40
    monkeypatch.setenv("GITHUB_SHA", merge_sha)

    def fake_command(*cmd, cwd=None):
        route = cmd[-1]
        if route.endswith("/pulls/386"):
            return json.dumps({"head": {"sha": head_sha}, "base": {"sha": stale_base}}).encode()
        if route.endswith(f"/commits/{merge_sha}"):
            return json.dumps({"parents": [{"sha": parent_sha}, {"sha": head_sha}]}).encode()
        if "/actions/workflows/ci.yml/runs" in route:
            return json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 99,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": parent_sha,
                            "head_repository": {"full_name": "codemyriad/cassini-gnome"},
                        }
                    ]
                }
            ).encode()
        if route.endswith("/actions/runs/99/artifacts?per_page=100"):
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 77,
                            "name": "ux-boards",
                            "expired": False,
                            "size_in_bytes": len(zip_bytes),
                            "digest": digest,
                        }
                    ]
                }
            ).encode()
        if route.endswith("/actions/runs/99/jobs"):
            return json.dumps(
                {"jobs": [{"id": 55, "name": "Boards / current-window", "labels": ["self-hosted", "cm-hetzner"]}]}
            ).encode()
        if route.endswith("/actions/jobs/55/logs"):
            return b"Runner Image\nVersion: 20261110.1.0\n"
        if route.endswith("/actions/artifacts/77/zip"):
            return zip_bytes
        raise AssertionError(cmd)

    monkeypatch.setattr(pr_shots, "command", fake_command)
    args = pr_shots.parser().parse_args(
        [
            "diff", "--layout", "gnome", "--repo", "codemyriad/cassini-gnome", "--pr", "386",
            "--base-sha", stale_base, "--head-sha", head_sha, "--head-runner", "self-hosted,cm-hetzner",
            "--after-dir", str(after_dir), "--base-workflow", "ci.yml", "--base-artifact", "ux-boards",
            "--out", str(tmp_path / "out-ok"),
        ]
    )  # fmt: skip
    res = pr_shots.diff(args)
    assert res["base"] == {
        "sha": parent_sha,
        "run_id": "99",
        "runner": "self-hosted,cm-hetzner",
        "image": "20261110.1.0",
    }
    assert not res["no_baseline"] and all(r["status"] == "unchanged" for r in res["rows"])

    # Missing baseline artifact reports no_baseline cleanly; missing --head-sha fails metadata validation.
    monkeypatch.setattr(pr_shots, "baseline", lambda _a: {})
    empty_args = pr_shots.parser().parse_args(
        [
            "diff", "--layout", "gnome", "--repo", "codemyriad/cassini-gnome", "--pr", "386",
            "--base-sha", parent_sha, "--head-sha", head_sha,
            "--after-dir", str(after_dir), "--base-workflow", "ci.yml", "--out", str(tmp_path / "out-none"),
        ]
    )  # fmt: skip
    no_base = pr_shots.diff(empty_args)
    assert no_base["no_baseline"] is True
    assert f"No main baseline for `{parent_sha}` yet" in (tmp_path / "out-none/block.md").read_text()

    bad_args = pr_shots.parser().parse_args(
        [
            "diff",
            "--layout",
            "gnome",
            "--repo",
            "codemyriad/cassini-gnome",
            "--pr",
            "386",
            "--before-dir",
            str(base_dir),
            "--after-dir",
            str(after_dir),
            "--out",
            str(tmp_path / "out-bad"),
        ]
    )
    with pytest.raises(ValueError, match="Invalid commit metadata"):
        pr_shots.diff(bad_args)


def test_ci_and_ux_shot_workflows_wire_pr_shots_read_only():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    ux = (ROOT / ".github/workflows/ux-shot.yml").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    boards_block = ci.split("\n  boards:\n", 1)[1].split("\n  library:\n", 1)[0]
    assert "permissions:\n      contents: read\n      actions: read" in boards_block
    assert "write" not in boards_block and "write" not in ux
    assert "permissions:\n  contents: read\n  actions: read" in ux
    assert "env -u GITHUB_STEP_SUMMARY python3 -I build-aux/ci/pr-shots.py --self-test" in ux
    for required in (
        "--layout gnome",
        '--base-sha "$BASE_SHA"',
        '--head-sha "$HEAD_SHA"',
        "wf=ci",
        '--base-workflow "$wf.yml"',
        "--base-artifact ux-boards",
        "name: pr-shots",
        "ux-artifacts/packages.txt",
    ):
        assert required in ux, required
    assert '"build-aux/ci/pr-shots.py"' in pyproject
