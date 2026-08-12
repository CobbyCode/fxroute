#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Structural checks for the extracted API modules:

1. every @router endpoint must decorate a handler function (name matches
   the route-handler naming pattern), never a helper;
2. no function or class method may be defined twice in one scope;
3. no bare reference to main.py runtime globals may remain unbound;
4. modules with an explicit runtime boundary must not import main.py.

Catches decorator/insertion drift, missed dependency bindings and regressions
to using main.py as a runtime service locator."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = ("spl_calibration.py", "library_api.py", "autosub.py", "measurement_session.py")
DECOUPLED_MODULES = ("playlist_io.py", "library_api.py", "spl_calibration.py", "playback_runtime.py")
MAIN_GLOBALS = {
    "SPOTIFY_PREARM_SAMPLE_RATE_HZ",
    "PIPEWIRE_HANDOFF_POLL_INTERVAL_MS",
    "current_track_info",
    "easyeffects_manager",
    "get_audio_output_overview",
    "get_samplerate_status",
    "library_scanner",
    "measurement_sr_session",
    "measurement_store",
    "player_instance",
    "playback_intent_generation",
    "playback_transition_coordinator",
    "settings",
    "subwoofer_runtime",
    "_audio_output_overview_with_effective_rate",
    "_begin_playback_transition_attempt",
    "_coordinator_current_playback_context",
    "_end_playback_transition_attempt",
    "_ensure_playback_samplerate_force",
    "_get_current_pipewire_force_rate",
    "_get_player_audio_samplerate",
    "_local_intent_matches_live_state",
    "_log_playback_graph_diagnosis",
    "_measurement_restore_intent_matches_live_state",
    "_path_within_root",
    "_playback_graph_diagnosis",
    "_pulse_suspend_sink_for_samplerate",
    "_reconcile_transition_sink_rate",
    "_require_easyeffects_manager",
    "_run_coordinated_transition",
    "_set_pipewire_force_rate",
    "_spotify_intent_matches_live_state",
    "_spotify_snapshot_identity_values",
    "_spotify_target_track_from_state",
    "_sync_subwoofer_runtime",
    "_sync_subwoofer_runtime_at_rate",
    "_wait_for_samplerate_alignment",
}
ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}
ROUTE_HANDLER_PREFIXES = (
    "get_", "set_", "list_", "create_", "update_", "export_", "remove_",
    "delete_", "upload_", "download_", "start_", "cancel_", "apply_", "measure_",
    "save_", "merge_",
)

errors: list[str] = []


def check_no_main_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "main":
            errors.append(f"{path.name}:{node.lineno} imports runtime state from main.py")
        elif isinstance(node, ast.Import) and any(alias.name == "main" for alias in node.names):
            errors.append(f"{path.name}:{node.lineno} imports main.py")


def check_duplicate_class_methods(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        definitions: dict[str, list[int]] = {}
        for node in class_node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions.setdefault(node.name, []).append(node.lineno)
        for name, lines in definitions.items():
            if len(lines) > 1:
                errors.append(
                    f"{path.name}:{class_node.name} duplicate method {name} at lines {lines}"
                )


def check_duplicate_module_functions(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, []).append(node.lineno)
    for name, lines in definitions.items():
        if len(lines) > 1:
            errors.append(f"{path.name}: duplicate function {name} at lines {lines}")


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
    def target_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(target_names(item) for item in target.elts))
        return set()

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
                bound.update(target_names(target))
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

for path in ROOT.glob("*.py"):
    check_duplicate_module_functions(path)
    check_duplicate_class_methods(path)

for mod in DECOUPLED_MODULES:
    check_no_main_imports(ROOT / mod)

if errors:
    print("ROUTER STRUCTURE CHECK FAILED")
    for err in errors:
        print(f"  {err}")
    sys.exit(1)

print("Structure check ok: routers, duplicate defs, runtime-global references, decoupled modules")
sys.exit(0)
