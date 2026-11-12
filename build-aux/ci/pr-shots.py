#!/usr/bin/env python3
# Vendored from codemyriad/cassini-ux@6c2aa0a62c6346a5ecea3782a0b7d899b7c616ae:tools/pr-shots/pr-shots.py
"""W0.8 screenshot comparisons. Diff is read-only; only the coordinator publishes."""

import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from PIL import Image, ImageChops, ImageOps

START, END = "<!-- cassini-shots:start -->", "<!-- cassini-shots:end -->"
KEY = re.compile(r"[a-z0-9][a-z0-9.-]{0,159}")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+")
Image.MAX_IMAGE_PIXELS = 12_000_000


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, timeout=120)


def safe_path(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
        raise ValueError("Unsafe image path")
    return p


def escaped(value):
    text = html.escape(" ".join(str(value).split())[:120], quote=True)
    return re.sub(r"([\\`|\[\]()!*_#~])", r"\\\1", text)


def key(row):
    parts = [row["screen"], row["scheme"]]
    parts += [row["lang"].lower()] if row["lang"] != "en" else []
    parts += [row["device"].lower()] if row["device"] not in {"desktop", "phone"} else []
    value = ".".join(parts)
    if not KEY.fullmatch(value):
        raise ValueError("Invalid screenshot identity: " + value)
    return value


def identity(path, layout, names=()):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    original, folders = parts[-1], parts[:-1]
    stem = original
    scale = re.search(r"@(\d+)x$", stem)
    if scale:
        stem = stem[: scale.start()]
    row = {
        "screen": "",
        "label": "",
        "scheme": "light",
        "lang": "en",
        "device": "phone" if layout in {"android", "ios"} else "desktop",
    }
    context = []
    for folder in folders:
        if folder == "ipad":
            row["device"] = "ipad"
        elif folder in {"light", "dark"}:
            row["scheme"] = folder
        elif re.fullmatch(r"(?:it|fr|es|pt-br|pt-pt)(?:-dark)?", folder.lower()):
            row["lang"] = folder.lower().removesuffix("-dark")
            if folder.endswith("-dark"):
                row["scheme"] = "dark"
        elif folder != "states":
            context.append(folder.removesuffix("-dark"))
            if folder.endswith("-dark"):
                row["scheme"] = "dark"
    scheme = locale = None
    # Consume at most one scheme and one locale, in either order. A second '-dark' is a state name.
    for _ in range(2):
        mode = re.search(r"-(light|dark)$", stem, re.IGNORECASE) if scheme is None else None
        language = (
            re.search(r"-(fr|it|es|pt-br|pt-pt)$", stem, re.IGNORECASE)
            if locale is None and layout in {"gnome", "mac"}
            else None
        )
        if mode:
            scheme, stem = mode[1].lower(), stem[: mode.start()]
        elif language:
            locale, stem = language[1].lower(), stem[: language.start()]
        else:
            break
    if scheme:
        row["scheme"] = scheme
    if locale:
        row["lang"] = locale
    if scheme == "light":
        at = original.lower().rfind("-light")
        sibling = PurePosixPath(path).with_name(original[:at] + original[at + 6 :] + ".png").as_posix()
        if sibling in names:
            stem += "-light"  # The unqualified snapshot is a separate fixture; retain both.
    stem = re.sub(r"^\d+-", "", stem).lower()
    if scale:
        stem += "-" + scale[1] + "x"
    row["screen"] = "-".join(context + [stem])
    row["label"] = row["screen"].replace("-", " ").capitalize()
    key(row)
    return row


def inventory(files, layout, peer=None, names=None):
    names = set(files) if names is None else names
    # Validate all payloads before selecting rows: additions, removals and unlisted images included.
    for path, raw in files.items():
        if path.endswith(".png"):
            decoded(raw).close()
    own = "shots.json" in files
    raw = files.get("shots.json", peer)
    manifest = json.loads(raw) if raw is not None else []
    rows = manifest.get("shots") if isinstance(manifest, dict) else manifest
    if not isinstance(rows, list):
        raise ValueError("shots.json must contain a shot list")
    explicit = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Manifest row requires an image path")
        path = str(safe_path(item["path"]))
        if not path.endswith(".png") or (own and path not in files):
            raise ValueError("Manifest image is missing: " + path)
        if path in explicit:
            raise ValueError("Duplicate manifest path: " + path)
        explicit[path] = item
    result = {}
    for path in sorted(p for p in files if p.endswith(".png")):
        if "screen" in explicit.get(path, {}):
            defaults = {
                "screen": explicit[path]["screen"],
                "label": "",
                "scheme": "dark" if "dark" in PurePosixPath(path).parts else "light",
                "lang": "en",
                "device": "ipad"
                if "ipad" in PurePosixPath(path).parts
                else "phone"
                if layout in {"android", "ios"}
                else "desktop",
            }
        else:
            defaults = identity(path, layout, names)
        row = defaults | explicit.get(path, {})
        row.pop("path", None)
        for field in ("screen", "scheme", "lang", "device"):
            if not isinstance(row.get(field), str):
                raise ValueError("Invalid screenshot identity field")
            row[field] = row[field].lower()
        row["lang"] = row["lang"].replace("_", "-")
        row["device"] = "phone" if row["device"] == "iphone" else row["device"]
        if explicit.get(path) is not None and "label" not in explicit[path]:
            row["label"] = row["screen"].replace("-", " ").capitalize()
        if row["scheme"] not in {"light", "dark"} or not isinstance(row["label"], str):
            raise ValueError("Invalid screenshot scheme/label")
        k = key(row)
        if k in result:
            raise ValueError("Duplicate screenshot identity: " + k)
        result[k] = row | {"path": path, "bytes": files[path]}
    return result


def directory(path, require_images=True):
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError(f"Directory not found: {path}")
    files, total = {}, 0
    for p in root.rglob("*"):
        if p.suffix == ".png" or p.name == "shots.json":
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):
                raise ValueError("Screenshot must be a regular file")
            total += p.stat().st_size
            if p.stat().st_size > 3_000_000 or total > 256_000_000 or len(files) >= 2000:
                raise ValueError("Screenshot input exceeds file/total limits")
            files[p.relative_to(root).as_posix()] = p.read_bytes()
    if require_images and not any(name.endswith(".png") for name in files):
        raise ValueError(f"No screenshots in directory: {path}")
    return files


def git_files(ref, root):
    root = str(safe_path(root))
    ref = command("git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    files, total = {}, 0
    entries = command("git", "ls-tree", "-rz", ref, "--", root).decode().split("\0")
    for entry in filter(None, entries):
        metadata, path = entry.split("\t", 1)
        if path.endswith(".png") or path == root + "/shots.json":
            mode, kind, oid = metadata.split()
            if mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError("Screenshot must be a regular git blob: " + path)
            size = int(command("git", "cat-file", "-s", oid))
            total += size
            if size > 3_000_000 or total > 256_000_000:
                raise ValueError("Committed screenshot input exceeds limits")
            files[str(PurePosixPath(path).relative_to(root))] = command("git", "show", ref + ":" + path)
    return ref, files


def decoded(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not a PNG")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds pixel limit")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGBA")


def compare(before, after, tolerance):
    a, b = decoded(before), decoded(after)
    size = max(a.width, b.width), max(a.height, b.height)
    if size[0] * size[1] > Image.MAX_IMAGE_PIXELS:
        raise ValueError("Padded comparison exceeds pixel limit")
    aa, bb = Image.new("RGBA", size), Image.new("RGBA", size)
    aa.paste(a, (0, 0))
    bb.paste(b, (0, 0))
    pa = Image.merge("RGBA", aa.convert("RGBa").split())
    pb = Image.merge("RGBA", bb.convert("RGBa").split())
    channels = ImageChops.difference(pa, pb).split()
    mask = channels[0].point(lambda n: 255 if n > tolerance else 0)
    for channel in channels[1:]:
        mask = ImageChops.lighter(mask, channel.point(lambda n: 255 if n > tolerance else 0))
    if a.size != b.size:
        present_a, present_b = Image.new("L", size), Image.new("L", size)
        present_a.paste(255, (0, 0, *a.size))
        present_b.paste(255, (0, 0, *b.size))
        mask = ImageChops.lighter(mask, ImageChops.difference(present_a, present_b))
    changed = mask.histogram()[255]
    heat = ImageOps.grayscale(pa).convert("RGB")
    heat.paste((220, 30, 45), mask=mask)
    return round(changed * 100 / (size[0] * size[1]), 6), mask.getbbox(), heat


def screen_id(row):
    return row["screen"], row["lang"], row["device"]


def sorted_rows(rows):
    core = {
        name: n for n, name in enumerate(("library", "recording", "meeting", "read-and-listen", "search", "settings"))
    }
    return sorted(rows, key=lambda r: (core.get(r["screen"], 99), screen_id(r), r["scheme"] != "light"))


def block(data, images=True):
    validate_metadata(data)
    rows = data["rows"]
    changed = {screen_id(r) for r in rows if r["status"] != "unchanged"}
    total = len({screen_id(r) for r in rows})
    lines = [START, "### UI screenshots", ""]
    base, head = data["base"], data["head"]
    if data.get("no_baseline"):
        lines.append(f"No main baseline for `{base['sha']}` yet (run pending or expired). After images only.")
    else:
        lines.append(
            f"Before: `{base['sha'][:7]}` (run {escaped(base['run_id'])}). "
            f"After: `{head['sha'][:7]}` (run {escaped(head['run_id'])})."
        )
    lines.append(
        f"Runners: {escaped(base['runner'])} / {escaped(head['runner'])}; "
        f"images: {escaped(base['image'])} / {escaped(head['image'])}."
    )
    if data.get("requested_base_sha", base["sha"]) != base["sha"]:
        lines.append(
            "Using the nearest older successful main baseline; requested " + data["requested_base_sha"][:7] + "."
        )
    if base["runner"] != head["runner"]:
        lines.append("Advisory: baseline and head runners differ.")
    if not changed:
        lines.append(f"No visual change in {total} screens (light and dark).")
    else:
        lines += [
            f"{len(changed)} screens changed, {total - len(changed)} unchanged.",
            "",
            "| Screen | Before | After | Changed |",
            "|---|---|---|---|",
        ]
        selected = sorted_rows([r for r in rows if screen_id(r) in changed])
        for index, row in enumerate(selected):
            if index == 12:
                lines += ["", f"<details><summary>{len(selected) - 12} more rows</summary>", ""]
            links = {}
            for side in ("before", "after", "diff"):
                path = row.get(side + "_git_path") if data.get("git_images") and side != "diff" else row.get(side)
                if not path:
                    links[side] = "–"
                    continue
                if data.get("git_images") and side != "diff":
                    url = f"https://github.com/{data['repo']}/raw/{data[side_map(side)]['sha']}/{quote(path, safe='/')}"
                else:
                    url = f"https://github.com/{data['repo']}/raw/pr-shots/pr-{data['pr']}/{head['sha'][:7]}/{path}"
                width = 220 if row["device"] in {"phone", "ipad"} else 400
                links[side] = (
                    f'<img src="{url}" width="{width}">'
                    if images and index < 12 and side != "diff"
                    else f"[{side}]({url})"
                )
            label = escaped(row["label"]) + " · " + row["scheme"]
            modes = {r["scheme"] for r in rows if screen_id(r) == screen_id(row)}
            if len(modes) == 1:
                label += " (" + row["scheme"] + " only)"
            pct = "–" if row["changed_pct"] is None else f"{row['changed_pct']:g} %"
            if row["diff"]:
                pct = links["diff"].replace("[diff]", f"[{pct}]")
            if index < 12:
                lines.append(f"| {label} | {links['before']} | {links['after']} | {pct} |")
            else:
                lines.append(f"- {label}: {links['before']} · {links['after']} · {pct}")
        if len(selected) > 12:
            lines += ["", "</details>"]
    if changed:
        url = f"https://github.com/{data['repo']}/tree/pr-shots/pr-{data['pr']}/{head['sha'][:7]}"
        if data.get("git_images"):
            url = f"https://github.com/{data['repo']}/tree/{head['sha']}/{quote(data['root'], safe='/')}"
        lines += ["", f"All images: [screenshots]({url})."]
    if data.get("ui_code_changed") and not changed:
        lines.append("UI code changed, no golden changed.")
    return "\n".join(lines + ["", "Made by `pr-shots.py`.", END, ""])


def side_map(side):
    return "base" if side == "before" else "head"


def diff(args):
    args.root = str(safe_path(args.root))
    base_sha, head_sha = args.base_sha, args.head_sha
    if args.git:
        base_sha, before = git_files(args.git[0], args.root)
        head_sha, after = git_files(args.git[1], args.root)
    else:
        before = directory(args.before_dir) if args.before_dir else baseline(args) if args.base_workflow else {}
        base_sha, head_sha = args.base_sha, args.head_sha
        after = directory(args.after_dir)
    names = set(before) | set(after)
    a = inventory(before, args.layout, peer=after.get("shots.json"), names=names)
    b = inventory(after, args.layout, peer=before.get("shots.json"), names=names)
    if not b or (args.before_dir is not None and not a):
        raise ValueError("Screenshot input inventory is empty")
    rows = []
    for k in sorted(a.keys() | b.keys()):
        left, right = a.get(k), b.get(k)
        row = {n: (right or left)[n] for n in ("screen", "label", "scheme", "lang", "device")}
        pct, bbox, _ = compare(left["bytes"], right["bytes"], args.tolerance) if left and right else (None, None, None)
        status = ("changed" if pct else "unchanged") if left and right else ("added" if right else "removed")
        rows.append(row | dict(status=status, changed_pct=pct, bbox=bbox, before=None, after=None, diff=None))
    changed = {screen_id(r) for r in rows if r["status"] != "unchanged"}
    pictures = {}
    for row in rows:
        if screen_id(row) not in changed:
            continue
        k = key(row)
        for side, item in (("before", a.get(k)), ("after", b.get(k))):
            if item:
                if args.git:
                    row[side + "_git_path"] = str(PurePosixPath(args.root) / item["path"])
                else:
                    row[side] = f"{side}/{k}.png"
                    pictures[row[side]] = item["bytes"]
        if not args.git and k in a and k in b:
            _, _, heat = compare(a[k]["bytes"], b[k]["bytes"], args.tolerance)
            row["diff"] = f"diff/{k}.png"
            encoded = io.BytesIO()
            heat.save(encoded, format="PNG")
            pictures[row["diff"]] = encoded.getvalue()
    data = {
        "version": 1,
        "app": args.layout,
        "repo": args.repo,
        "pr": args.pr,
        "git_images": bool(args.git),
        "no_baseline": args.before_dir is None and not args.git and not before,
        "rows": rows,
        "ui_code_changed": False,
        "root": args.root,
        "requested_base_sha": getattr(args, "requested_base_sha", base_sha),
    }
    for side, sha in (("base", base_sha), ("head", head_sha)):
        data[side] = {
            "sha": sha,
            "run_id": getattr(args, side + "_run"),
            "runner": getattr(args, side + "_runner"),
            "image": getattr(args, side + "_image"),
        }
    validate_metadata(data)
    if args.git:
        data["ui_code_changed"] = bool(
            command("git", "diff", "--name-only", base_sha, head_sha, "--", "app/src/main", "ui/src/main")
        )
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty (avoid stale screenshots)")
    output.mkdir(parents=True, exist_ok=True)
    for name, raw in pictures.items():
        path = output / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
    (output / "changes.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (output / "block.md").write_text(block(data), encoding="utf-8")
    summary = block(data, images=False)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)
    print(summary)
    return data


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        for folder in ("a/light", "a/dark", "b/light", "b/dark"):
            (p / folder).mkdir(parents=True)
            Image.new("RGBA", (3, 2), "white").save(p / folder / "library.png")
        image = Image.open(p / "b/light/library.png").convert("RGBA")
        image.putpixel((1, 1), (0, 0, 0, 255))
        image.save(p / "b/light/library.png")
        args = parser().parse_args(
            [
                "diff",
                "--layout",
                "gnome",
                "--repo",
                "owner/app",
                "--pr",
                "1",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--before-dir",
                str(p / "a"),
                "--after-dir",
                str(p / "b"),
                "--out",
                str(p / "out"),
            ]
        )
        data = diff(args)
        assert len(data["rows"]) == 2 and (p / "out/after/library.dark.png").exists()
        validated(data, directory(p / "out"), "owner/app", 1, "b" * 40)
        body = replace_block("Description", block(data))
        assert replace_block(body, block(data)) == body
        assert next(r for r in data["rows"] if r["scheme"] == "light")["bbox"] == (1, 1, 2, 2)
    print("pr-shots.py self-test passed")


def github(route):
    return json.loads(command("gh", "api", "repos/" + route))


def artifact_files(repo, artifact):
    size = artifact["size_in_bytes"]
    if not 0 < size <= 256_000_000:
        raise ValueError("Artifact exceeds download limit")
    raw = command("gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
    if len(raw) != size or (
        (artifact.get("digest") or "").startswith("sha256:")
        and hashlib.sha256(raw).hexdigest() != artifact["digest"][7:]
    ):
        raise ValueError("Artifact size or digest mismatch")
    result, total = {}, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            if not info.filename.endswith((".png", "shots.json", "changes.json")):
                continue
            name = str(safe_path(info.filename))
            if name in result or info.external_attr >> 28 == 0xA or info.file_size > 3_000_000:
                raise ValueError("Unsafe or oversized artifact member")
            total += info.file_size
            if len(result) >= 2000 or total > 256_000_000:
                raise ValueError("Artifact expansion limit")
            result[name] = archive.read(info)
    return result


def find_artifact(repo, run_id, name):
    artifacts = github(f"{repo}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    return next((a for a in artifacts if a["name"] == name and not a["expired"]), None)


def baseline(args):
    pr = github(f"{args.repo}/pulls/{args.pr}")
    args.head_sha = args.head_sha or pr["head"]["sha"]
    args.base_sha = args.base_sha or pr["base"]["sha"]
    merge = os.environ.get("GITHUB_SHA")
    if merge and merge != args.head_sha:
        parents = github(f"{args.repo}/commits/{merge}")["parents"]
        if len(parents) == 2 and parents[1]["sha"] == args.head_sha:
            args.base_sha = parents[0]["sha"]
    args.requested_base_sha = args.base_sha
    runs = github(f"{args.repo}/actions/workflows/{args.base_workflow}/runs?branch=main&status=success&per_page=100")[
        "workflow_runs"
    ]
    for run in sorted(runs, key=lambda r: r["head_sha"] != args.base_sha):
        if run.get("event") not in {"push", "schedule", "workflow_dispatch"}:
            continue
        if run["head_branch"] != "main" or (run.get("head_repository") or {}).get("full_name") != args.repo:
            continue
        if run["head_sha"] != args.base_sha:
            relation = github(f"{args.repo}/compare/{run['head_sha']}...{args.base_sha}")["status"]
            if relation not in {"ahead", "identical"}:
                continue
        artifact = find_artifact(args.repo, run["id"], args.base_artifact)
        if artifact:
            args.base_sha, args.base_run = run["head_sha"], str(run["id"])
            jobs = github(f"{args.repo}/actions/runs/{run['id']}/jobs")["jobs"]
            job = next((j for j in jobs if "board" in j["name"].lower()), jobs[0])
            args.base_runner = ",".join(job.get("labels", [])) or "unknown"
            log = command("gh", "api", f"repos/{args.repo}/actions/jobs/{job['id']}/logs").decode(errors="replace")
            image = re.search(r"(?:Image Version|Version):\s*(\d{8}(?:\.\d+)+)", log)
            args.base_image = image[1] if image else args.base_image
            files = artifact_files(args.repo, artifact)
            if not any(name.endswith(".png") for name in files):
                raise ValueError("Baseline artifact contains no screenshots")
            return files
    return {}


def validate_metadata(data):
    repo, pr = data.get("repo"), data.get("pr")
    if (
        not isinstance(repo, str)
        or not REPO.fullmatch(repo)
        or repo.split("/")[1] in {".", ".."}
        or type(pr) is not int
        or pr < 1
    ):
        raise ValueError("Invalid repository/PR metadata")
    if data.get("version") != 1 or data.get("app") not in {"gnome", "mac", "ios", "android"}:
        raise ValueError("Invalid screenshot format")
    for side in ("base", "head"):
        meta = data.get(side)
        if not isinstance(meta, dict) or not isinstance(meta.get("sha"), str) or not SHA.fullmatch(meta["sha"]):
            raise ValueError("Invalid commit metadata")
        if not all(isinstance(meta.get(n), (str, int)) for n in ("run_id", "runner", "image")):
            raise ValueError("Invalid runner metadata")
    requested = data.get("requested_base_sha", data["base"]["sha"])
    if not isinstance(requested, str) or not SHA.fullmatch(requested):
        raise ValueError("Invalid requested baseline")
    git_images = data.get("git_images", False)
    if type(git_images) is not bool or (git_images and data["app"] != "android"):
        raise ValueError("Invalid git image mode")
    if "root" in data or git_images:
        if not isinstance(data.get("root"), str):
            raise ValueError("Invalid image root")
        safe_path(data["root"])
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        raise ValueError("Invalid shot inventory")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(n), str) for n in ("screen", "label", "scheme", "lang", "device")
        ):
            raise ValueError("Invalid screenshot text")
        k = key(row)
        if (
            k in seen
            or row["scheme"] not in {"light", "dark"}
            or row.get("status") not in {"added", "removed", "changed", "unchanged"}
        ):
            raise ValueError("Invalid or duplicate screenshot row")
        seen.add(k)
        pct = row.get("changed_pct")
        if pct is not None and (type(pct) not in {int, float} or not math.isfinite(pct) or not 0 <= pct <= 100):
            raise ValueError("Invalid percentage")
        bbox = row.get("bbox")
        if bbox is not None and (
            not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(type(v) is int and v >= 0 for v in bbox)
        ):
            raise ValueError("Invalid bounding box")
        for side in ("before", "after", "diff"):
            value = row.get(side)
            if value is not None and (not isinstance(value, str) or value != f"{side}/{k}.png" or git_images):
                raise ValueError("Invalid screenshot path")
        for side in ("before", "after"):
            path = row.get(side + "_git_path")
            if path is not None:
                if not git_images or not isinstance(path, str) or not path.endswith(".png"):
                    raise ValueError("Invalid committed image path")
                relative = safe_path(path)
                if not relative.is_relative_to(PurePosixPath(data["root"])):
                    raise ValueError("Committed image escapes root")
    changed = {screen_id(r) for r in rows if r["status"] != "unchanged"}
    for row in rows:
        sides = [bool(row.get(s + "_git_path" if git_images else s)) for s in ("before", "after")]
        expected = {
            "added": [False, True],
            "removed": [True, False],
            "changed": [True, True],
            "unchanged": [True, True],
        }
        if sides != (expected[row["status"]] if screen_id(row) in changed else [False, False]):
            raise ValueError("Image references do not match screenshot status")
        if screen_id(row) not in changed and row.get("diff"):
            raise ValueError("Unchanged screen has a dangling diff")


def validated(data, files, repo, pr, head):
    validate_metadata(data)
    if data["repo"] != repo or data["pr"] != pr or data["head"]["sha"] != head:
        raise ValueError("Folder provenance does not match this PR head")
    if data.get("git_images"):
        return {}
    needed = {r[s] for r in data["rows"] for s in ("before", "after", "diff") if r.get(s)}
    if len(needed) > 80:
        raise ValueError("Publisher limit: 80 PNG files")
    for name in needed:
        raw = files[name]
        if len(raw) > 3_000_000:
            raise ValueError("Oversized PNG")
        decoded(raw).close()
    return {name: files[name] for name in needed}


def replace_block(body, replacement):
    if START not in body and END not in body:
        return body.rstrip() + "\n\n" + replacement
    if body.count(START) != 1 or body.count(END) != 1 or body.index(START) > body.index(END):
        raise ValueError("Unbalanced screenshot markers")
    return body[: body.index(START)] + replacement.rstrip() + body[body.index(END) + len(END) :]


def checkout_host(repo, root):
    url = f"https://github.com/{repo}.git"
    refs = github(f"{repo}/git/matching-refs/heads/pr-shots")
    old = next((r["object"]["sha"] for r in refs if r["ref"] == "refs/heads/pr-shots"), None)
    if old:
        command(
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "clone",
            "--quiet",
            "--single-branch",
            "--depth=1",
            "--branch=pr-shots",
            url,
            str(root),
        )
    else:
        root.mkdir()
        command("git", "init", "--quiet", "--initial-branch=pr-shots", cwd=root)
        command("git", "remote", "add", "origin", url, cwd=root)
    return old


def push_host(root, old=None, squash=False):
    if squash:
        command("git", "checkout", "--quiet", "--orphan", "pr-shots-squash", cwd=root)
    command("git", "add", "--all", cwd=root)
    # An identical publish is a no-op, not an error or another history entry.
    if not command("git", "diff", "--cached", "--name-only", cwd=root) and not squash:
        return
    command("git", "commit", "--quiet", "--allow-empty", "-m", "Update PR screenshots", cwd=root)
    lease = [f"--force-with-lease=refs/heads/pr-shots:{old}"] if squash else []
    command(
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        *lease,
        "origin",
        "HEAD:refs/heads/pr-shots",
        cwd=root,
    )


def publish(args):
    pr = github(f"{args.repo}/pulls/{args.pr}")
    head = pr["head"]["sha"]
    if args.dir:
        files = directory(args.dir, require_images=False)
        manifest = args.dir / "changes.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1_000_000:
            raise ValueError("Unsafe changes.json")
        files["changes.json"] = manifest.read_bytes()
    else:
        runs = (
            [github(f"{args.repo}/actions/runs/{args.run}")]
            if args.run
            else github(f"{args.repo}/actions/runs?head_sha={head}&per_page=100")["workflow_runs"]
        )
        files = None
        for run in runs:
            if (
                run["head_sha"] == head
                and run["head_branch"] == pr["head"]["ref"]
                and (run.get("head_repository") or {}).get("full_name") == pr["head"]["repo"]["full_name"]
            ):
                artifact = find_artifact(args.repo, run["id"], "pr-shots")
                if artifact:
                    files = artifact_files(args.repo, artifact)
                    break
        if files is None:
            raise ValueError("No unexpired pr-shots artifact for the current PR head")
    if len(files["changes.json"]) > 1_000_000:
        raise ValueError("Oversized changes.json")
    data = json.loads(files["changes.json"])
    needed = validated(data, files, args.repo, args.pr, head)
    rendered = block(data)  # Never use the artifact's block.md.
    print(rendered)
    if args.dry_run:
        return
    if needed:
        with tempfile.TemporaryDirectory(prefix="pr-shots-publish-") as tmp:
            root = Path(tmp) / "repo"
            checkout_host(args.repo, root)
            target = root / f"pr-{args.pr}" / head[:7]
            if target.parent.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("Unsafe host directory")
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            for name, raw in needed.items():
                path = target / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(raw)
            (target / "published.json").write_text(
                json.dumps({"head": head, "at": datetime.now(timezone.utc).isoformat()})
            )
            push_host(root)
    latest = github(f"{args.repo}/pulls/{args.pr}")
    if latest["head"]["sha"] != head:
        raise ValueError("PR head changed during publication; body left untouched")
    body = replace_block(latest.get("body") or "", rendered)
    if len(body.encode()) > 65_000:
        raise ValueError("PR body exceeds GitHub limit")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "body.md"
        path.write_text(body, encoding="utf-8")
        command("gh", "pr", "edit", str(args.pr), "--repo", args.repo, "--body-file", str(path))


def prune(args):
    with tempfile.TemporaryDirectory(prefix="pr-shots-prune-") as tmp:
        root = Path(tmp) / "repo"
        old = checkout_host(args.repo, root)
        if old is None:
            return
        over = sum(p.stat().st_size for p in root.rglob("*.png")) > 200_000_000
        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        for folder in root.glob("pr-*"):
            if not re.fullmatch(r"pr-[1-9][0-9]*", folder.name) or folder.is_symlink():
                raise ValueError("Unexpected host folder")
            pr = github(f"{args.repo}/pulls/{folder.name[3:]}")
            heads = list(folder.iterdir())
            if any(p.is_symlink() or not re.fullmatch(r"[0-9a-f]{7}", p.name) for p in heads):
                raise ValueError("Unexpected head folder")
            newest = max(heads, key=lambda p: json.loads((p / "published.json").read_text())["at"], default=None)
            expired = (
                over and pr.get("merged_at") and datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) < cutoff
            )
            for path in heads:
                keep = path == newest and not expired and (pr["state"] == "open" or pr.get("merged_at"))
                print(("keep " if keep else "drop ") + str(path.relative_to(root)))
                if not keep:
                    shutil.rmtree(path)
        if not args.dry_run:
            push_host(root, old, squash=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="mode")
    d = sub.add_parser("diff")
    d.add_argument("--layout", choices=("android", "ios", "mac", "gnome"), required=True)
    d.add_argument("--repo", required=True)
    d.add_argument("--pr", type=int, required=True)
    d.add_argument("--git", nargs=2)
    d.add_argument("--root", default="ui/src/test/screenshots")
    d.add_argument("--before-dir", type=Path)
    d.add_argument("--after-dir", "--head-dir", type=Path)
    d.add_argument("--base-workflow")
    d.add_argument("--base-artifact", default="ux-boards")
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--tolerance", type=int, default=2)
    for side in ("base", "head"):
        d.add_argument(f"--{side}-sha", default="")
        d.add_argument(f"--{side}-run", default=os.environ.get("GITHUB_RUN_ID", "local") if side == "head" else "local")
        d.add_argument(f"--{side}-runner", default="unknown")
        d.add_argument(
            f"--{side}-image", default=os.environ.get("ImageVersion", "unknown") if side == "head" else "unknown"
        )
    for name in ("publish", "prune"):
        c = sub.add_parser(name)
        c.add_argument("--repo", required=True)
        c.add_argument("--dry-run", action="store_true")
        if name == "publish":
            c.add_argument("--pr", type=int, required=True)
            source = c.add_mutually_exclusive_group()
            source.add_argument("--dir", type=Path)
            source.add_argument("--run", type=int)
    return p


def main():
    args = parser().parse_args()
    if args.self_test:
        self_test()
    elif args.mode in {"publish", "prune"}:
        if not REPO.fullmatch(args.repo) or (args.mode == "publish" and args.pr < 1):
            raise ValueError("Invalid repository/PR")
        (publish if args.mode == "publish" else prune)(args)
    elif args.mode == "diff":
        if not 0 <= args.tolerance <= 255 or (not args.git and not args.after_dir):
            raise ValueError("Supply --git or --after-dir, and tolerance 0..255")
        diff(args)
    else:
        parser().error("Use diff or --self-test")


if __name__ == "__main__":
    main()
