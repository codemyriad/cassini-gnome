#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""List every repository file the Boards job reads (G, D20).

    build-aux/ci/boards_inputs.py                    # one path per line
    build-aux/ci/boards_inputs.py --missing          # inputs Boards' PR filter misses
    build-aux/ci/boards_inputs.py --why PATH         # how the scan reached PATH
    build-aux/ci/boards_inputs.py --patterns tests/  # the fewest globs for them

Boards' PR path filter in pr-paths.json must match each of these files, or a
PR changing only that file would skip Boards (tests/test_ci_orchestrator.py
checks it). The scan is static and errs on the side of listing too much:

- Seeds: the Boards workflow (ux-shot.yml) and the workflow that builds the
  product it tests (build.yml).
- Workflows and shell scripts: every word, outside comments, naming a file
  from the repository root (the job's working directory) or the script's own
  directory; a word with a "/" may also name a directory, read as a whole.
- Python: every module it imports (absolute ones from its own directory or
  any directory a scanned file puts on sys.path, rescanned until no new
  sys.path entry appears; relative ones from its package). Every string
  constant and every `base / "a" / "b"` chain naming a file or, for a chain
  or a word with a "/", a directory read as a whole. A bare file name inside
  prose ("see README.md") names the file's neighbour only. Docstrings and
  sys.path entries themselves are not inputs.
- Pytest: a scanned test_*.py or conftest.py also reads pyproject.toml and
  each conftest.py from its directory up to the root.
- Meson: the product's meson.build files; each subdir() is read as a whole,
  and so is each string naming a file or directory beside the meson.build,
  outside test() registrations (the Meson tests job, not the product).

A path built at run time from variables is invisible to the scan: name such
a file as a constant in the script that reads it.
"""

import argparse
import ast
import fnmatch
import functools
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEEDS = (".github/workflows/ux-shot.yml", ".github/workflows/build.yml")
PRODUCT_MESON = "meson.build"
WORD = re.compile(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*")
COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.M)
SKIP_PARTS = {"__pycache__", ".git"}


def relative(path, root=ROOT):
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return None if not rel.parts or SKIP_PARTS & set(rel.parts) else rel.as_posix()


@functools.cache
def tree(directory, root=ROOT):
    """Every file under a directory, as repository paths."""
    return frozenset(
        {rel for path in (root / directory).rglob("*") if path.is_file() and (rel := relative(path, root)) is not None}
    )


def resolve(word, bases, root=ROOT):
    """The repository files, and directories, a word names from any base.

    Only a word with a "/" names a directory: a bare "tests" is just a word,
    while chains (`ROOT / "tests"`) reach here as "tests/".
    """
    found = set()
    word = word.strip()
    directories = "/" in word
    for candidate in {word, word.lstrip("/")}:
        if not candidate or candidate in {".", ".."} or len(candidate) > 255 or any(c.isspace() for c in candidate):
            continue
        for base in bases:
            path = root / base / candidate
            if (path.is_file() or directories and path.is_dir()) and (rel := relative(path, root)) is not None:
                found.add(rel)
    return found


def constant_chain(node):
    """The constant parts of `x / "a" / "b"` joined ("a/b"), or None."""
    parts = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return None
        parts.append(node.right.value)
        node = node.left
    return "/".join(reversed(parts)) if parts else None


def is_sys_path(node):
    if isinstance(node, ast.Subscript):
        node = node.value
    return isinstance(node, ast.Attribute) and node.attr == "path" and getattr(node.value, "id", None) == "sys"


def sys_path_nodes(tree_):
    """The expressions a module puts on sys.path."""
    for node in ast.walk(tree_):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append", "extend"}
            and is_sys_path(node.func.value)
        ):
            yield from node.args
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(is_sys_path(target) for target in targets):
                yield node.value


def words_in(node):
    docstrings = {
        id(child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)
    }
    chained = set()
    for child in ast.walk(node):
        if id(child) in chained:
            continue
        if (chain := constant_chain(child)) is not None:
            # Only the whole chain names a path: skip its inner links.
            link = child
            while isinstance(link, ast.BinOp):
                chained |= {id(link), id(link.right)}
                link = link.left
            yield chain + "/"
        elif isinstance(child, ast.Constant) and isinstance(child.value, str) and id(child) not in docstrings:
            if WORD.fullmatch(child.value):
                yield child.value
            else:
                # A command line or message names files among other words; a
                # bare file name in prose ("see README.md") is the file's
                # neighbour, not the repository's.
                yield from (word if "/" in word else "./" + word for word in WORD.findall(child.value))


@functools.cache
def python_references(path, root=ROOT):
    """(modules, words, sys.path words) of one Python file.

    A relative import is returned as a module of the file's own package,
    prefixed with "." per level.
    """
    source = ast.parse(path.read_text(), str(path))
    modules = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            modules.add(base)
            modules.update(f"{base}.{alias.name}" if node.module else base + alias.name for alias in node.names)
    roots = {word for value in sys_path_nodes(source) for word in words_in(value)}
    return modules, set(words_in(source)) - roots, roots


def module_files(name, search, root=ROOT):
    """The files importing a dotted name loads from any search directory."""
    found = set()
    parts = name.split(".")
    for base in search:
        for depth in range(1, len(parts) + 1):
            stem = root / base / Path(*parts[:depth])
            for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                if candidate.is_file() and (rel := relative(candidate, root)) is not None:
                    found.add(rel)
    return found


def without_calls(text, name):
    """Text with every name(...) call removed, parentheses balanced."""
    out, index = [], 0
    for match in re.finditer(rf"(?<![A-Za-z0-9_.]){name}\(", text):
        if match.start() < index:
            continue
        out.append(text[index : match.start()])
        depth, index = 0, match.end() - 1
        while index < len(text):
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            index += 1
            if depth == 0:
                break
    return "".join(out) + text[index:]


def meson_inputs(root=ROOT):
    files, pending, seen = set(), [PRODUCT_MESON], set()
    while pending:
        meson = pending.pop()
        if meson in seen or not (root / meson).is_file():
            continue
        seen.add(meson)
        files.add(meson)
        here = Path(meson).parent.as_posix()
        # Meson's test() registrations run in the Meson tests job; they are
        # not part of the installed product.
        text = without_calls(COMMENT.sub("", (root / meson).read_text()), "test")
        for directory in re.findall(r"subdir\(\s*'([^']+)'", text):
            sub = (Path(here) / directory).as_posix()
            files |= tree(sub, root)
            pending.append(f"{sub}/meson.build")
        for word in re.findall(r"'([^'\n]+)'", text):
            for rel in resolve(word, [here], root):
                files |= tree(rel, root) if (root / rel).is_dir() else {rel}
    return files


def scan(search, root=ROOT, origin=None):
    """(files, import roots) reached from the seeds with these import roots.

    origin, if given, maps each file to the (file, word) that first named it.
    """
    origin = {} if origin is None else origin
    files = meson_inputs(root)
    if (root / "meson_options.txt").is_file():
        files.add("meson_options.txt")
    origin.update((rel, ("meson", "")) for rel in files)
    origin.update((rel, ("seed", "")) for rel in SEEDS)
    pending, scanned, roots = [*SEEDS, *sorted(files)], set(), set()
    while pending:
        rel = pending.pop(0)
        if rel in scanned or not (root / rel).is_file():
            continue
        scanned.add(rel)
        files.add(rel)
        path, here = root / rel, Path(rel).parent.as_posix()
        found = {}
        if path.suffix == ".py":
            modules, words, path_words = python_references(path, root)
            roots |= {d for word in path_words for d in resolve(word, [".", here], root) if (root / d).is_dir()}
            for word in sorted(words):
                bases = [here] if word.startswith("./") else [".", here]
                found.update(dict.fromkeys(resolve(word.removeprefix("./"), bases, root), word))
            for name in sorted(modules):
                if name.startswith("."):
                    level = len(name) - len(name.lstrip("."))
                    package = Path(rel).parents[level - 1].as_posix()
                    hits = module_files(name.lstrip("."), {package}, root) if name.lstrip(".") else set()
                else:
                    hits = module_files(name, {here, *search}, root)
                found.update(dict.fromkeys(hits, "import " + name))
            if path.name.startswith("test_") or path.name == "conftest.py":
                if (root / "pyproject.toml").is_file():
                    found["pyproject.toml"] = "pytest"
                for parent in Path(rel).parents:
                    if (root / parent / "conftest.py").is_file():
                        found[(parent / "conftest.py").as_posix()] = "pytest"
        elif path.suffix in {".sh", ".yml", ".yaml"}:
            for word in WORD.findall(COMMENT.sub("", path.read_text())):
                found.update(dict.fromkeys(resolve(word, [".", here], root), word))
        for item, word in found.items():
            for reached in sorted(tree(item, root)) if (root / item).is_dir() else [item]:
                if reached not in origin:
                    origin[reached] = (rel, word)
                files.add(reached)
                pending.append(reached)
    return files, roots


def inputs(root=ROOT, origin=None):
    """Every repository file the Boards job and its product read, sorted."""
    search = set()
    while True:
        # sys.path is shared by the process: an import root any scanned file
        # adds can resolve any other file's imports, so rescan until stable.
        trace = {}
        files, roots = scan(search, root, trace)
        if roots <= search:
            if origin is not None:
                origin.update(trace)
            return sorted(files)
        search |= roots


def patterns(files, root=ROOT):
    """The fewest globs matching exactly these files.

    The topmost directory all of whose files are listed becomes directory/**.
    """
    files = set(files)
    covered = {}

    def complete(directory):
        if directory not in covered:
            covered[directory] = tree(directory, root) <= files
        return covered[directory]

    out = set()
    for path in files:
        whole = None
        for parent in reversed(Path(path).parents[:-1]):
            if complete(parent.as_posix()):
                whole = parent.as_posix() + "/**"
                break
        out.add(whole or path)
    return sorted(out)


def triggers_everything(path):
    # changes.py runs every area for these, Boards included.
    return path.startswith((".github/", "build-aux/ci/"))


def missing(globs, files):
    """The files a PR could change without running Boards under these globs."""
    return [
        path
        for path in files
        if not triggers_everything(path) and not any(fnmatch.fnmatchcase(path, glob) for glob in globs)
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--missing", action="store_true", help="only the inputs Boards' PR filter does not match")
    parser.add_argument("--why", metavar="PATH", help="the chain of files and words that reaches PATH")
    parser.add_argument("--patterns", metavar="PREFIX", help="the fewest globs for inputs under PREFIX (tests/)")
    args = parser.parse_args()
    origin = {}
    files = inputs(origin=origin)
    if args.why:
        path = args.why
        while path in origin and path not in {"seed", "meson"}:
            source, word = origin[path]
            print(f"{path}  <-  {source}" + (f"  ({word})" if word else ""))
            path = source
        return 0 if args.why in origin else 1
    if args.patterns is not None:
        files = patterns(path for path in files if path.startswith(args.patterns))
    elif args.missing:
        policy = json.loads((ROOT / "build-aux/ci/pr-paths.json").read_text())
        files = missing(policy["paths"]["boards"], files)
    print("\n".join(files))
    return 1 if args.missing and files else 0


if __name__ == "__main__":
    sys.exit(main())
