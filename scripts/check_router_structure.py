#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Structural checks for the extracted API modules:

1. every @router endpoint must decorate a handler function (name matches
   the route-handler naming pattern), never a helper;
2. no function may be defined twice in one module;
3. no bare reference to main.py runtime globals may remain unbound
   (each function must bind them via a lazy `from main import ...` or the
   module must define/import the name at module level).

Catches decorator/insertion drift and missed lazy imports like a route
decorator landing on a helper or a function using `settings` without
importing it."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = ("spl_calibration.py", "library_api.py", "autosub.py")
MAIN_GLOBALS = {
    "easyeffects_manager",
    "library_scanner",
    "measurement_sr_session",
    "measurement_store",
    "settings",
    "subwoofer_runtime",
    "_read_measurement_setup_settings",
    "_require_easyeffects_manager",
    "_resolve_measurement_start_sample_rate",
    "_sync_subwoofer_runtime_for_measurement_sweep",
}
ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}
ROUTE_HANDLER_PREFIXES = (
    "get_", "set_", "list_", "create_", "update_", "export_", "remove_",
    "delete_", "upload_", "download_", "start_", "cancel_", "apply_", "measure_",
)

errors: list[str] = []


def module_level_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def bound_names(node: ast.FunctionDef | ast.AsyncFunctionDef, module_level: set[str]) -> set[str]:
    bound = set()
    for arg in node.args.args + node.args.kwonlyargs:
        bound.add(arg.arg)
    if node.args.vararg:
        bound.add(node.args.vararg.arg)
    if node.args.kwarg:
        bound.add(node.args.kwarg.arg)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
            bound.add(sub.target.id)
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            if isinstance(sub.target, ast.Name):
                bound.add(sub.target.id)
        elif isinstance(sub, ast.comprehension):
            if isinstance(sub.target, ast.Name):
                bound.add(sub.target.id)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                bound.add(alias.asname or alias.name.split(".")[0])
    return bound | module_level


for mod in MODULES:
    path = ROOT / mod
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level = module_level_names(tree)
    definitions: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions[node.name] = definitions.get(node.name, 0) + 1
            is_route = any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) in ROUTE_METHODS
                for d in node.decorator_list
            )
            if is_route and not node.name.startswith(ROUTE_HANDLER_PREFIXES):
                errors.append(f"{mod}:{node.lineno} route decorator on unexpected function {node.name}")
            used = {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}
            unbound = sorted(n for n in used & MAIN_GLOBALS if n not in bound_names(node, module_level))
            if unbound:
                errors.append(f"{mod}:{node.lineno} {node.name}: unbound main global(s) {unbound}")
    for name, count in definitions.items():
        if count > 1:
            errors.append(f"{mod}: duplicate definition of {name} ({count}x)")

if errors:
    print("ROUTER STRUCTURE CHECK FAILED")
    for err in errors:
        print(f"  {err}")
    sys.exit(1)

print("Router structure check ok: endpoint decorators, duplicate defs, unbound main globals")
sys.exit(0)
