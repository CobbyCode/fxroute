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
MAIN = ROOT / "main.py"
SOURCE = MAIN.read_text()
TREE = ast.parse(SOURCE)

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
    "ensure_stereo_output_graph",
    "load_preset",
    "set_exact_sub_mute",
}

PLAYBACK_ENTRYPOINTS = {
    "play_track",
    "pause_playback",
    "toggle_playback",
    "_load_queue_track",
    "_advance_playback_queue",
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
    "play_track", "toggle_playback", "_load_queue_track",
    "_advance_playback_queue", "api_spotify_play", "api_spotify_toggle",
    "_request_coordinated_recovery", "_release",
}


def _function_names() -> set[str]:
    return {
        node.name
        for node in ast.walk(TREE)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls() -> list[tuple[int, str, str]]:
    result: list[tuple[int, str, str]] = []

    def walk(node: ast.AST, stack: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + (node.name,)
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in MUTATION_CALL_NAMES:
                result.append((node.lineno, "/".join(stack) or "<module>", name))
            if name == "to_thread" and node.args:
                delegated = node.args[0]
                delegated_name = None
                if isinstance(delegated, ast.Name):
                    delegated_name = delegated.id
                elif isinstance(delegated, ast.Attribute):
                    delegated_name = delegated.attr
                if delegated_name in {"_set_hardware_sink_mute", "ensure_stereo_output_graph"}:
                    result.append((node.lineno, "/".join(stack) or "<module>", delegated_name))
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    walk(TREE)
    return sorted(result)


def _reason(context: str, name: str) -> str | None:
    leaf = context.rsplit("/", 1)[-1]
    if leaf in PLAYBACK_ENTRYPOINTS:
        return None
    if (
        leaf in {
            "establish_target_rate", "prepare_target_source", "set_hardware_mute",
            "_complete_playback_handoff", "_rollback_playback_handoff",
            "_repair_playback_graph_once", "_repair_stereo_output_links_once",
            "_coordinator_establish_effects_and_helper",
            "_relink_post_start_missing_production_links",
            "rollback_output_mode_runtime",
            "_ensure_mpv_to_easyeffects_links",
            "_sync_easyeffects_preset_for_playback_samplerate",
        }
        or "_sync_locked" in context
    ):
        return "Coordinator adapter/core: production transition graph ownership"
    if leaf == "stabilize_effects_after_rate_change":
        return "Coordinator adapter/core: bounded link-only repair under the closed gate"
    if leaf == "_ensure_stereo_easyeffects_output_graph":
        return "central output-graph adapter; reached from helper sync in Coordinator or startup/config sync"
    if leaf == "_reconcile_transition_sink_rate":
        return "rate reconciliation primitive invoked by Coordinator adapter/verifier or measurement entry"
    if leaf == "_coordinator_reconcile_subwoofer_links_only":
        return "Coordinator-only link reconciliation; no helper/process restart"
    if leaf == "reconcile_measurement_session_graph":
        return "Coordinator measurement-session link-only reconciliation"
    if leaf in {"_easyeffects_output_ports_present", "_playback_graph_diagnosis"}:
        return "read-only graph diagnosis/readback"
    if leaf in {
        "_start_locked", "_release", "_sync_subwoofer_runtime_for_measurement_sweep",
        "_sync_subwoofer_runtime_at_rate", "start_measurement", "start_lr_repeat_measurement",
        "_restore_auto_sub_original_config", "_measure_auto_sub_candidate",
        "_run_auto_sub_22_optimize", "_run_auto_sub_22_stereo_optimize", "_run_auto_sub_optimize",
    }:
        return "measurement/AutoSub workflow, outside playback transitions"
    if leaf in {
        "lifespan", "save_audio_output_selection_route", "save_audio_output_mode_route",
        "_finish_easyeffects_preset_mutation", "save_easyeffects_extras",
        "load_easyeffects_preset",
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

    for line, context, name in _calls():
        reason = _reason(context, name)
        if reason is None:
            errors.append(f"unclassified direct mutation at main.py:{line}: {context} -> {name}")

    for entrypoint in sorted(PLAYBACK_ENTRYPOINTS):
        nodes = [
            node
            for node in ast.walk(TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint
        ]
        if not nodes:
            errors.append(f"playback entrypoint missing: {entrypoint}")
            continue
        body = ast.get_source_segment(SOURCE, nodes[0]) or ""
        if entrypoint in TRANSPORT_ONLY_ENTRYPOINTS:
            if "_run_coordinated_transition" in body:
                errors.append(f"transport endpoint enters coordinator: {entrypoint}")
            for mutation in MUTATION_CALL_NAMES:
                if re.search(rf"\b{re.escape(mutation)}\s*\(", body):
                    errors.append(
                        f"transport endpoint directly mutates playback graph: {entrypoint} -> {mutation}"
                    )
        elif "_run_coordinated_transition" not in body and entrypoint != "_advance_playback_queue":
            errors.append(f"playback entrypoint bypasses coordinator: {entrypoint}")
        for old in OLD_HANDOFF_NAMES:
            if old in body:
                errors.append(f"{entrypoint} still references obsolete path {old}")

    for path_name in sorted(SINGLE_OWNER_PATHS):
        nodes = [
            node
            for node in ast.walk(TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == path_name
        ]
        if not nodes:
            errors.append(f"single-owner path missing: {path_name}")
            continue
        body = ast.get_source_segment(SOURCE, nodes[0]) or ""
        if path_name == "_advance_playback_queue":
            if "_load_queue_track" not in body:
                errors.append("auto-advance does not enter the coordinator-backed queue loader")
        elif path_name == "_release":
            for required in (
                "playback_transition_coordinator.restore_measurement",
                "playback_restore_via_coordinator",
                "measurement_only_restore",
            ):
                if required not in body:
                    errors.append(f"measurement restore missing single-owner guard: {required}")
        elif "_run_coordinated_transition" not in body:
            errors.append(f"single-owner path bypasses coordinator: {path_name}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("Playback mutation ownership audit: PASS")
    for line, context, name in _calls():
        print(f"  main.py:{line} {context} -> {name}: {_reason(context, name)}")
    print("  Hardware mute writer: only FxrouteTransitionRuntime.set_hardware_mute")
    print("  Force-rate writer: _ensure_playback_samplerate_force plus guarded measurement owner")
    print("  Single-owner paths: source entry, Resume, Queue, Auto-Advance, Replay, Radio, Spotify handoff, Recovery, Measurement-Restore")
    print("  Transport-only paths: MPV pause toggle, Spotify pause/next/previous")
    print("  Legacy handoff/prearm/drift wrappers: removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
