#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Dead-symbol scan over every tracked production .py file.

A module-level def/class/assign whose name does not occur in any other
tracked file is reported. Byte-count based, so it is deliberately
conservative: names matching inside other identifiers (``run`` inside
``runtime``) are never reported, dynamic or decorator-based registration
that keeps the name in another file is seen, and module orphans loaded via
relative/intermediate imports need a manual grep. Every candidate must be
verified by hand (grep for import forms and dynamic dispatch) before any
removal.
"""
import ast
import subprocess
from collections import defaultdict
from pathlib import Path

# Decorators that register the function by reference; the bare name never
# needs to appear anywhere else for the symbol to be alive.
REGISTERING_DECORATORS = {
    "post", "get", "put", "delete", "patch", "websocket", "on_event",
    "add_event_handler", "exception_handler", "middleware",
    "validator", "field_validator", "model_validator",
}

tracked = subprocess.run(["git", "ls-files", "*.py"], capture_output=True,
                         text=True, check=True).stdout.split()
blobs = {}
for path in tracked:
    try:
        blobs[path] = Path(path).read_bytes()
    except OSError:
        pass

all_bytes = b"".join(blobs.values())


def external_count(name: str, defining_path: str) -> int:
    needle = name.encode()
    return all_bytes.count(needle) - blobs[defining_path].count(needle)


results = defaultdict(list)
for path in tracked:
    if path.startswith(("scripts/", "tests/", "docs/")) or "/test" in path \
            or path.startswith("test_"):
        continue
    try:
        tree = ast.parse(blobs[path].decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        continue
    local_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            local_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            local_names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            local_names.add(node.value)
    exported = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        exported = {elt.value for elt in node.value.elts}
                    except Exception:
                        pass
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            registered = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr in REGISTERING_DECORATORS
                for d in node.decorator_list)
            if registered:
                continue
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in names:
            if name.startswith("__") or name in exported:
                continue
            if name in local_names:
                continue
            if external_count(name, path) == 0:
                results[path].append((node.lineno, name))

for path in sorted(results):
    for line, name in results[path]:
        print(f"{path}:{line}: {name}")
print(f"--- {sum(len(v) for v in results.values())} candidates ---")
