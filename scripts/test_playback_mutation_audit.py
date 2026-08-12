#!/usr/bin/env python3
"""Static ownership audit for playback graph/rate mutations.

The low-level PipeWire/EasyEffects/helper primitives remain in their existing
modules.  This audit checks the application callsites: source-entry and graph
mutations must submit to PlaybackTransitionCoordinator, while same-source
transport endpoints may call only their transport primitive.  Startup,
configuration and measurement workflows are explicitly classified as separate
owners.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Application callsite scope: main.py, the extracted playback runtime and the
# extracted AutoSub module.  Playback entrypoints and single-owner paths live
# only in main.py; all files are parsed for direct mutation calls so the
# extraction cannot create an audit coverage gap.
AUDIT_FILES = ("main.py", "playback_runtime.py", "playback_queue.py", "autosub.py", "measurement_session.py")
SOURCES = {name: (ROOT / name).read_text() for name in AUDIT_FILES}
TREES = {name: ast.parse(source) for name, source in SOURCES.items()}
MAIN_SOURCE = SOURCES["main.py"]
TREE = TREES["main.py"]

OLD_HANDOFF_NAMES = {
    "_legacy_play_track",
    "_legacy_toggle_playback",
    "_legacy_load_queue_track",
    "_complete_local_playback_handoff",
    "_complete_radio_handoff_after_load",
    "_complete_spotify_entry_handoff",
    "_sync_subwoofer_runtime_after_playback_transition",
    "_prearm_known_local_samplerate",
    "_prearm_spotify_samplerate",
    "_recover_spotify_samplerate_alignment",
    "_bounce_easyeffects_preset_for_samplerate_recovery",
    "_maybe_repair_active_app_samplerate_drift",
    "_repair_active_app_samplerate_drift_locked",
    "_maybe_recover_spotify_samplerate_mismatch",
    "_complete_playback_handoff",
    "_rollback_playback_handoff",
    "_repair_playback_graph_once",
    "_should_use_mpv_native_queue",
    "_prime_mpv_native_queue",
    "_trim_mpv_native_queue",
}

MUTATION_CALL_NAMES = {
    "_set_pipewire_force_rate",
    "_ensure_playback_samplerate_force",
    "_sync_easyeffects_preset_for_playback_samplerate",
    "_sync_subwoofer_runtime",
    "_sync_subwoofer_runtime_at_rate",
    "_sync_subwoofer_runtime_for_measurement_sweep",
    "_run_pw_link_command",
    "_connect_ports",
    "_disconnect_ports",
    "_ensure_mpv_to_easyeffects_links",
    "_repair_stereo_output_links_once",
    "_set_hardware_sink_mute",
    "load_preset",
    "set_exact_sub_mute",
    "_load_easyeffects_preset",
}

# Mutations delegated through worker wrappers (asyncio.to_thread and the
# cancellation-safe _run_locked_worker) instead of being called directly.
DELEGATED_MUTATION_CALL_NAMES = {"_set_hardware_sink_mute"}

PLAYBACK_ENTRYPOINTS = {
    "play_track",
    "pause_playback",
    "toggle_playback",
    "load_track",
    "advance",
    "api_spotify_play",
    "api_spotify_pause",
    "api_spotify_toggle",
    "api_spotify_next",
    "api_spotify_previous",
}

TRANSPORT_ONLY_ENTRYPOINTS = {
    "pause_playback",
    "api_spotify_pause",
    "api_spotify_next",
    "api_spotify_previous",
}

SINGLE_OWNER_PATHS = {
    "play_track", "toggle_playback", "load_track",
    "advance", "api_spotify_play", "api_spotify_toggle",
    "_request_coordinated_recovery", "_release",
}

# Queue-module entrypoints reach the Coordinator exclusively through the
# injected run_transition boundary (the queue module must never call the
# main.py facade or know transition stages).
QUEUE_MODULE_ENTRYPOINTS = {"load_track", "advance"}

# Queue state values owned exclusively by playback_queue.PlaybackQueue.
QUEUE_STATE_GLOBALS = {
    "playback_queue",
    "playback_queue_original",
    "playback_queue_index",
    "playback_queue_mode",
    "playback_queue_loop",
    "playback_queue_shuffle",
    "single_track_loop",
}

# Deps fields removed from PlaybackRuntimeDependencies in Pass 2; the runtime
# reaches the queue exclusively through the typed queue boundary.
REMOVED_RUNTIME_QUEUE_DEPS = {
    "get_queue_mode",
    "set_queue_mode",
    "get_queue",
    "get_queue_index",
    "get_queue_loop",
    "get_single_track_loop",
    "reduce_native_mpv_playlist_to_current",
    "reset_mpv_loop_state",
}


def _function_names() -> set[str]:
    return {
        node.name
        for tree in TREES.values()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _calls() -> list[tuple[str, int, str, str]]:
    result: list[tuple[str, int, str, str]] = []

    def walk(node: ast.AST, file_name: str, stack: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + (node.name,)
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in MUTATION_CALL_NAMES:
                result.append((file_name, node.lineno, "/".join(stack) or "<module>", name))
            delegated = None
            if name == "to_thread" and node.args:
                delegated = node.args[0]
            elif name == "_run_locked_worker" and len(node.args) >= 2:
                delegated = node.args[1]
            if delegated is not None:
                delegated_name = None
                if isinstance(delegated, ast.Name):
                    delegated_name = delegated.id
                elif isinstance(delegated, ast.Attribute):
                    delegated_name = delegated.attr
                if delegated_name in DELEGATED_MUTATION_CALL_NAMES:
                    result.append((file_name, node.lineno, "/".join(stack) or "<module>", delegated_name))
        for child in ast.iter_child_nodes(node):
            walk(child, file_name, stack)

    for file_name, tree in TREES.items():
        walk(tree, file_name)
    return sorted(result)


def _reason(context: str, name: str) -> str | None:
    leaf = context.rsplit("/", 1)[-1]
    if leaf == "make_playback_runtime_deps":
        return "late-bound runtime wiring factory; the referenced mutations are never executed here"
    if leaf in PLAYBACK_ENTRYPOINTS:
        return None
    if (
        leaf in {
            "establish_target_rate", "prepare_target_source", "set_hardware_mute",
            "_complete_playback_handoff", "_rollback_playback_handoff",
            "_repair_playback_graph_once", "_repair_stereo_output_links_once",
            "_coordinator_establish_effects_and_helper",
            "_relink_missing_production_links",
            "rollback_output_mode_runtime",
            "_ensure_mpv_to_easyeffects_links",
            "_sync_easyeffects_preset_for_playback_samplerate",
            "_restore_committed_source_after_failed_transition",
        }
        or "_sync_locked" in context
    ):
        return "Coordinator adapter/core: production transition graph ownership"
    if leaf == "stabilize_effects_after_rate_change":
        return "Coordinator adapter/core: bounded link-only repair under the closed gate"
    if leaf == "_ensure_stereo_easyeffects_output_graph":
        return "targeted stereo link repair after helper sync; no preset or service restart"
    if leaf == "_reconcile_transition_sink_rate":
        return "rate reconciliation primitive invoked by Coordinator adapter/verifier or measurement entry"
    if leaf == "_coordinator_reconcile_subwoofer_links_only":
        return "Coordinator-only link reconciliation; no helper/process restart"
    if leaf == "reconcile_measurement_session_graph":
        return "Coordinator measurement-session link-only reconciliation"
    if leaf in {"_easyeffects_output_ports_present", "_mpv_source_ports_present", "_playback_graph_diagnosis"}:
        return "read-only graph diagnosis/readback"
    if leaf in {
        "_start_locked", "_release", "_sync_subwoofer_runtime_for_measurement_sweep",
        "_sync_subwoofer_runtime_at_rate", "start_measurement", "start_lr_repeat_measurement",
    }:
        return "measurement workflow, outside playback transitions"
    if leaf in {
        "_measure_auto_sub_candidate", "_capture_auto_sub_main_references",
        "_measure_auto_sub_combined_candidate", "_run_auto_sub_optimize",
        "_run_auto_sub_22_optimize", "_run_auto_sub_22_stereo_optimize",
    }:
        return "AutoSub sweep workflow, outside playback transitions"
    if leaf in {
        "lifespan", "save_audio_output_selection_route", "save_audio_output_mode_route",
        "_finish_easyeffects_preset_mutation", "save_easyeffects_extras",
        "load_easyeffects_preset", "_load_easyeffects_preset",
    }:
        return "startup or explicit user configuration workflow"
    if leaf.startswith("sync_peak_monitor_for_"):
        return "peak-monitor process only; no production graph mutation"
    if leaf in {"_disconnect_external_input_source", "_ensure_external_input_loopback", "_link_bluetooth_source_to_easyeffects"}:
        return "external-input/Bluetooth routing workflow"
    if leaf in {"_disconnect_ports", "_connect_ports"}:
        return "shared PipeWire port primitive; callers are Coordinator graph repair or input/config workflows"
    if leaf in {"write_force_rate"} and "_ensure_playback_samplerate_force" in context:
        return "rate reconciliation primitive invoked by Coordinator or measurement owner"
    return None


def main() -> int:
    errors: list[str] = []
    names = _function_names()
    for old in sorted(OLD_HANDOFF_NAMES):
        if old in names:
            errors.append(f"obsolete handoff function still defined: {old}")

    for file_name, line, context, name in _calls():
        reason = _reason(context, name)
        if reason is None:
            errors.append(f"unclassified direct mutation at {file_name}:{line}: {context} -> {name}")

    for entrypoint in sorted(PLAYBACK_ENTRYPOINTS):
        entrypoint_source = ""
        nodes = []
        for fname, tree in TREES.items():
            matches = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint
            ]
            if matches:
                nodes = matches
                entrypoint_source = SOURCES[fname]
                break
        if not nodes:
            errors.append(f"playback entrypoint missing: {entrypoint}")
            continue
        body = ast.get_source_segment(entrypoint_source, nodes[0]) or ""
        if entrypoint in TRANSPORT_ONLY_ENTRYPOINTS:
            if "_run_coordinated_transition" in body:
                errors.append(f"transport endpoint enters coordinator: {entrypoint}")
            for mutation in MUTATION_CALL_NAMES:
                if re.search(rf"\b{re.escape(mutation)}\s*\(", body):
                    errors.append(
                        f"transport endpoint directly mutates playback graph: {entrypoint} -> {mutation}"
                    )
        elif "_run_coordinated_transition" not in body and entrypoint not in QUEUE_MODULE_ENTRYPOINTS:
            errors.append(f"playback entrypoint bypasses coordinator: {entrypoint}")
        for old in OLD_HANDOFF_NAMES:
            if old in body:
                errors.append(f"{entrypoint} still references obsolete path {old}")

    for path_name in sorted(SINGLE_OWNER_PATHS):
        nodes: list[ast.AST] = []
        owning_source = ""
        owning_file = ""
        for fname, tree in TREES.items():
            matches = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == path_name
            ]
            if matches:
                nodes = matches
                owning_source = SOURCES[fname]
                owning_file = fname
                break
        if not nodes:
            errors.append(f"single-owner path missing: {path_name}")
            continue
        body = ast.get_source_segment(owning_source, nodes[0]) or ""
        if path_name == "advance":
            if "load_track" not in body:
                errors.append("auto-advance does not enter the coordinator-backed queue loader")
        elif path_name == "_release":
            for required in (
                "playback_transition_coordinator.restore_measurement",
                "playback_restore_via_coordinator",
                "measurement_only_restore",
            ):
                if required not in body:
                    errors.append(f"measurement restore missing single-owner guard: {required}")
        elif owning_file == "playback_queue.py":
            # The queue module reaches the Coordinator only through the
            # injected run_transition callable; it never calls the main.py
            # facade or knows transition stages.
            if "run_transition" not in body:
                errors.append(f"single-owner path bypasses coordinator boundary: {path_name}")
        elif "_run_coordinated_transition" not in body:
            errors.append(f"single-owner path bypasses coordinator: {path_name}")

    # Queue-state ownership (Pass 2): the seven queue values may only be
    # defined inside playback_queue.py; main.py keeps only queue_advancing.
    main_tree = TREES["main.py"]
    main_module_names = _module_level_names(main_tree)
    for name in sorted(QUEUE_STATE_GLOBALS):
        if name in main_module_names:
            errors.append(f"main.py still defines queue state global: {name}")
    if "queue_advancing" not in main_module_names:
        errors.append("main.py no longer defines the queue_advancing dispatcher guard")

    queue_tree = TREES["playback_queue.py"]
    for node in ast.walk(queue_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "main":
            errors.append("playback_queue.py imports runtime state from main.py")
        elif isinstance(node, ast.Import) and any(alias.name == "main" for alias in node.names):
            errors.append("playback_queue.py imports main.py")

    runtime_tree = TREES["playback_runtime.py"]
    runtime_fields: set[str] = set()
    for class_node in (
        node for node in ast.walk(runtime_tree) if isinstance(node, ast.ClassDef)
    ):
        if class_node.name == "PlaybackRuntimeDependencies":
            for field in class_node.body:
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                    runtime_fields.add(field.target.id)
    for name in sorted(REMOVED_RUNTIME_QUEUE_DEPS):
        if name in runtime_fields:
            errors.append(f"PlaybackRuntimeDependencies still declares queue dep: {name}")
    if "queue" not in runtime_fields:
        errors.append("PlaybackRuntimeDependencies no longer exposes the typed queue boundary")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("Playback mutation ownership audit: PASS")
    for file_name, line, context, name in _calls():
        print(f"  {file_name}:{line} {context} -> {name}: {_reason(context, name)}")
    print("  Hardware mute writer: only FxrouteTransitionRuntime.set_hardware_mute")
    print("  Force-rate writer: _ensure_playback_samplerate_force plus guarded measurement owner")
    print("  Single-owner paths: source entry, Resume, Queue, Auto-Advance, Replay, Radio, Spotify handoff, Recovery, Measurement-Restore")
    print("  Transport-only paths: MPV pause toggle, Spotify pause/next/previous")
    print("  Legacy handoff/prearm/drift wrappers: removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
