#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Structural check: every @router endpoint in the extracted API modules
must decorate a function whose name matches the route handler, and no
handler may be defined twice. Catches decorator/insertion drift like a
route decorator landing on a helper function."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = ("spl_calibration.py", "library_api.py", "autosub.py")
errors: list[str] = []

for mod in MODULES:
    path = ROOT / mod
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defs: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = defs.get(node.name, 0) + 1
            route = any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) in ("get", "post", "put", "delete", "patch")
                for d in node.decorator_list
            )
            if route and not node.name.startswith(("get_", "set_", "list_", "create_", "update_", "export_", "remove_", "delete_", "upload_", "download_", "start_", "cancel_", "apply_", "measure_")):
                errors.append(f"{mod}: route decorator on unexpected function {node.name}")

    for name, count in defs.items():
        if count > 1:
            errors.append(f"{mod}: duplicate definition of {name} ({count}x)")

if errors:
    print("ROUTER STRUCTURE CHECK FAILED")
    for err in errors:
        print(f"  {err}")
    sys.exit(1)

print("Router structure check ok: endpoints + helpers correctly placed, no duplicates")
sys.exit(0)
