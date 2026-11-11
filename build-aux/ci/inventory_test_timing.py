#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative D88 discovery inventory; candidates require human classification."""

import ast
import bisect
import collections
import csv
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLOCK_FUNCTIONS = {
    *(
        f"time.{name}{suffix}"
        for name in ("monotonic", "perf_counter", "time", "clock_gettime", "thread_time", "process_time")
        for suffix in ("", "_ns")
    ),
    "GLib.get_monotonic_time",
    "GLib.get_real_time",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.now",
    "datetime.utcnow",
    "timeit.timeit",
    "timeit.repeat",
    "timeit.default_timer",
}
CLOCK = re.compile(
    r"\b(?:" + "|".join(map(re.escape, sorted(CLOCK_FUNCTIONS | {"Date.now", "performance.now"}))) + r")\s*\("
)
SLEEP = re.compile(
    r"\b(?:time|asyncio)\.sleep\s*\(|\b(?:sleep|waitForTimeout)\s*\(|\bThread\.sleep\s*\(|\bsleep\s+[.0-9]"
)
TIMER = re.compile(r"\b(?:GLib\.timeout_add(?:_seconds)?|threading\.Timer|setTimeout|setInterval)\s*\(")
ASSERT_WORDS = re.compile(
    r"elapsed|exit_seconds|deadline|latency|heartbeat|perf_counter|monotonic|wall_time|wallTime|cpu_gaps|retry_gaps|frame_gaps",
    re.I,
)
SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".kt", ".c", ".html"}


def test_source(path):
    return (
        not {"node_modules", ".venv"}.intersection(pathlib.PurePosixPath(path).parts)
        and pathlib.PurePosixPath(path).suffix in SUFFIXES
        and (
            path.startswith(("tests/", "browser-protocol/scenarios/", "browser-protocol/conformance/"))
            or "/tests/" in path
            or re.search(r"(?:^|/)(?:test_[^/]+\.py|[^/]+\.test\.[cm]?js)$", path) is not None
        )
    )


def propagate(edges):
    """Worklist closure, including chains longer than an arbitrary pass count."""
    dependents = collections.defaultdict(set)
    for targets, dependencies in edges:
        for dependency in dependencies:
            dependents[dependency].update(targets)
    tainted, pending = {"$clock"}, collections.deque(["$clock"])
    while pending:
        for target in dependents[pending.popleft()]:
            if target not in tainted:
                tainted.add(target)
                pending.append(target)
    return tainted


def python_sites(text, add):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    nodes = list(ast.walk(tree))
    aliases, bindings = {}, []
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = node.module + "." + alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.extend((target, node.value) for target in targets)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            bindings.extend(zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=False))
            bindings.extend(
                (arg, default)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False)
                if default is not None
            )

    def qualified(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id).removeprefix("gi.repository.")
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    # Resolve aliases of modules, functions, other aliases and default arguments.
    # This intentionally merges scopes: false positives are review candidates.
    pending = list(bindings)
    while pending:
        unresolved, changed = [], False
        for target, value in pending:
            name = target.id if isinstance(target, ast.Name) else target.arg if isinstance(target, ast.arg) else None
            canonical = qualified(value)
            if name and (
                canonical in CLOCK_FUNCTIONS | {"time.sleep", "asyncio.sleep"}
                or canonical in ("time", "asyncio", "datetime", "GLib", "timeit")
            ):
                if aliases.get(name) != canonical:
                    aliases[name] = canonical
                    changed = True
            else:
                unresolved.append((target, value))
        if not changed:
            break
        pending = unresolved

    def names(node):
        return (
            {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
            | {qualified(item) for item in ast.walk(node) if isinstance(item, ast.Attribute)}
            | ({node.arg} if isinstance(node, ast.arg) else set())
        )

    def targets(node):
        # A timed self.started field must not taint every other self.field.
        if isinstance(node, (ast.Name, ast.Attribute)):
            return {qualified(node)}
        if isinstance(node, ast.arg):
            return {node.arg}
        if isinstance(node, (ast.Tuple, ast.List)):
            return set().union(*(targets(item) for item in node.elts))
        return names(node)

    def dependencies(node):
        result = names(node)
        if any(isinstance(item, ast.Call) and qualified(item.func) in CLOCK_FUNCTIONS for item in ast.walk(node)):
            result.add("$clock")
        return result

    edges = [(targets(target), dependencies(value)) for target, value in bindings]
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "extend", "update")
        ):
            edges.append((targets(node.func.value), set().union(*(dependencies(arg) for arg in node.args))))
    tainted = propagate(edges)
    for node in nodes:
        if isinstance(node, ast.Call):
            function = qualified(node.func)
            if function in CLOCK_FUNCTIONS:
                add(node.lineno, "clock-read")
            if function in ("time.sleep", "asyncio.sleep"):
                add(node.lineno, "fixed-delay")
        if isinstance(node, ast.Assert) and dependencies(node.test) & tainted:
            add(node.lineno, "temporal-assertion")
        if (
            isinstance(node, (ast.If, ast.While))
            and dependencies(node.test) & tainted
            and any(
                isinstance(item, ast.Compare)
                and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in item.ops)
                for item in ast.walk(node.test)
            )
        ):
            add(node.lineno, "clock-condition")


# A dependency-free lexical scan, not a JavaScript parser. Strings/comments are
# opaque; balanced expressions retain multiline callbacks/objects and locations.
JS_TOKEN = re.compile(
    r"""//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|(?:\d+(?:\.\d*)?)|===|!==|=>|==|!=|<=|>=|\+=|-=|&&|\|\||[^\s]"""
)
IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")


def javascript_sites(text, add):
    tokens, offsets = [], []
    for match in JS_TOKEN.finditer(text):
        value = match[0]
        if value.startswith(("//", "/*", "'", '"', "`")):
            continue
        tokens.append(value)
        offsets.append(match.start())
    line_starts = [0, *(match.end() for match in re.finditer("\n", text))]
    pairs, stack = {}, []
    for index, value in enumerate(tokens):
        if value in ("(", "[", "{"):
            stack.append(index)
        elif value in (")", "]", "}") and stack and tokens[stack[-1]] == {")": "(", "]": "[", "}": "{"}[value]:
            start = stack.pop()
            pairs[start] = index

    def report(index, kind):
        add(bisect.bisect_right(line_starts, offsets[index]), kind)

    def names(start, end):
        return {value for value in tokens[start:end] if IDENTIFIER.fullmatch(value)}

    def direct(start, end):
        return any(
            tokens[index : index + 3] in (["Date", ".", "now"], ["performance", ".", "now"])
            for index in range(start, end - 2)
        )

    def dependencies(start, end):
        return names(start, end) | ({"$clock"} if direct(start, end) else set())

    def expression_end(start):
        index = start
        while index < len(tokens):
            if tokens[index] in (";", ",", ")", "]", "}"):
                break
            index = pairs.get(index, index) + 1
        return index

    reverse_pairs = {end: start for start, end in pairs.items()}

    def target_root(index):
        while index >= 0:
            if tokens[index] == "]" and index in reverse_pairs:
                index = reverse_pairs[index] - 1
            elif index >= 2 and tokens[index - 1] == ".":
                index -= 2
            else:
                return tokens[index] if IDENTIFIER.fullmatch(tokens[index]) else None
        return None

    def binding_targets(index):
        if tokens[index] in ("]", "}") and index in reverse_pairs:
            start = reverse_pairs[index]
            if tokens[index] == "}" or start == 0 or tokens[start - 1] in ("const", "let", "var", "(", ","):
                return names(start + 1, index)
        root = target_root(index)
        return {root} if root else set()

    edges, alias_edges = [], []
    for index, value in enumerate(tokens):
        if value in ("=", "+=", "-=") and index:
            targets = binding_targets(index - 1)
            end = expression_end(index + 1)
            edges.append((targets, dependencies(index + 1, end)))
            if direct(index + 1, end) and end - index == 4:
                alias_edges.append((targets, {"$clock"}))
            elif end - index == 2 and IDENTIFIER.fullmatch(tokens[index + 1]):
                alias_edges.append((targets, {tokens[index + 1]}))
        # Track container mutation and iteration: continuity.push(...) and
        # for (const measure of continuity) assert(measure.error < bound).
        if (
            value == "("
            and index >= 3
            and tokens[index - 1] in ("push", "unshift", "splice")
            and tokens[index - 2] == "."
        ):
            root = target_root(index - 3)
            if root:
                edges.append(({root}, dependencies(index + 1, pairs.get(index, index))))
        if value == "of" and index and index + 1 < len(tokens):
            end = expression_end(index + 1)
            edges.append((binding_targets(index - 1), dependencies(index + 1, end)))
    tainted = propagate(edges)
    aliases = propagate(alias_edges) - {"$clock"}
    for index, value in enumerate(tokens):
        if value != "(" or index not in pairs:
            continue
        end = pairs[index]
        start = index - 1
        while start >= 2 and tokens[start - 1] == ".":
            start -= 2
        function = ".".join(tokens[start:index:2])
        if function in ("Date.now", "performance.now") or function in aliases:
            report(start, "clock-read")
        if (
            tokens[start] == "assert"
            or tokens[start].startswith("assertLess")
            or tokens[start].startswith("assertGreater")
        ) and dependencies(index + 1, end) & tainted:
            report(start, "temporal-assertion")
        if (
            tokens[index - 1] in ("if", "while", "for")
            and dependencies(index + 1, end) & tainted
            and any(value in ("<", "<=", ">", ">=") for value in tokens[index + 1 : end])
        ):
            report(index - 1, "clock-condition")
        if tokens[index - 1] == "Promise" and index >= 2 and tokens[index - 2] == "new":
            for child in range(index + 1, end):
                if tokens[child : child + 2] == ["setTimeout", "("]:
                    report(child, "fixed-delay")


def scan_text(path, text):
    rows, lines = {}, text.splitlines()

    def add(line, kind):
        rows[(line, kind)] = [
            path,
            line,
            kind,
            "vendored" if path.startswith(("browser-protocol/", "browser-extension/")) else "gnome",
            "review",
            " ".join(lines[line - 1].split()),
        ]

    for number, line in enumerate(lines, 1):
        for pattern, kind in ((CLOCK, "clock-read"), (SLEEP, "fixed-delay"), (TIMER, "timer-callback")):
            if pattern.search(line):
                add(number, kind)
        if (
            re.search(r"\bassert(?:\.|\s|\().*", line)
            and ASSERT_WORDS.search(line)
            and re.search(r"[<>]|\b(?:assertLess|assertGreater)", line)
        ):
            add(number, "temporal-assertion")
        if re.search(r"\.(?:wait|join|communicate|select)\(\s*(?:timeout\s*=\s*)?[0-9]|\btimeout\s*=", line):
            add(number, "bounded-wait")
    if path.endswith(".py"):
        python_sites(text, add)
    elif pathlib.PurePosixPath(path).suffix in (".js", ".mjs", ".cjs"):
        javascript_sites(text, add)
    return [rows[key] for key in sorted(rows)]


def inventory(root):
    paths = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    files = [path for path in paths if test_source(path)]
    rows = [row for path in files for row in scan_text(path, (root / path).read_text(errors="replace"))]
    return files, sorted(rows)


def main():
    files, rows = inventory(ROOT)
    with (ROOT / "docs/test-timing-inventory.csv").open("w") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["path", "line", "kind", "owner", "status", "expression"])
        writer.writerows(rows)
    print(
        "files scanned",
        len(files),
        "sites",
        len(rows),
        "affected files",
        len({row[0] for row in rows}),
        "kinds",
        dict(collections.Counter(row[2] for row in rows)),
    )
    print("owned", sum(row[3] == "gnome" for row in rows), "vendored", sum(row[3] == "vendored" for row in rows))


if __name__ == "__main__":
    main()
