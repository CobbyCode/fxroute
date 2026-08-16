#!/usr/bin/env python3

"""Static contracts for coordinator ownership boundaries."""

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = {
    file_name: (ROOT / file_name).read_text()
    for file_name in ("main.py", "playback/orchestration.py", "playback/queue.py", "measurement/session.py", "dsp/orchestration.py")
}
TREES = {
    file_name: ast.parse(source)
    for file_name, source in SOURCES.items()
}


def function_source(name):
    preferred = {
        "_coordinator_target_rate", "_sample_rate_policy_is_auto",
        "_transition_sample_rate_policy", "_coordinator_current_playback_context", "_coordinator_rate_change",
    }
    owner_methods = {
        "_coordinator_target_rate": "coordinator_target_rate",
        "_sample_rate_policy_is_auto": "sample_rate_policy_is_auto",
        "_transition_sample_rate_policy": "transition_sample_rate_policy",
        "_coordinator_current_playback_context": "current_playback_context",
        "_coordinator_rate_change": "coordinator_rate_change",
    }
    lookup_name = owner_methods.get(name, name)
    file_names = list(SOURCES)
    if name in preferred:
        file_names.remove("playback/orchestration.py")
        file_names.insert(0, "playback/orchestration.py")
    for file_name in file_names:
        source = SOURCES[file_name]
        lines = source.splitlines(keepends=True)
        for node in ast.walk(TREES[file_name]):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == lookup_name:
                return "".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"function not found: {name}")


class OwnershipStructureTests(unittest.TestCase):
    def test_public_samplerate_endpoint_is_read_only(self):
        body = function_source("audio_samplerate_status")
        self.assertNotIn("_maybe_repair_active_app_samplerate_drift", body)
        self.assertNotIn("_set_pipewire_force_rate", body)
        self.assertNotIn("dsp_orchestrator.sync_runtime", body)

    def test_coordinated_playback_entrypoints_do_not_prearm_or_bypass_coordinator(self):
        for name in (
            "play_track",
            "toggle_playback",
            "load_track",
            "api_spotify_play",
            "api_spotify_toggle",
        ):
            body = function_source(name)
            self.assertNotIn("_prearm_known_local_samplerate", body, name)
            self.assertNotIn("_prearm_spotify_samplerate", body, name)
            if name == "load_track":
                # The queue module reaches the Coordinator exclusively through
                # the injected run_transition boundary.
                self.assertIn("run_transition", body, name)
            else:
                self.assertIn("_run_coordinated_transition", body, name)

    def test_same_source_transport_paths_do_not_enter_coordinator_or_mutate_graph(self):
        forbidden = (
            "_run_coordinated_transition",
            "_set_pipewire_force_rate",
            "_ensure_playback_samplerate_force",
            "dsp_orchestrator.sync_preset_for_playback_samplerate",
            "dsp_orchestrator.sync_runtime",
            "_ensure_mpv_to_dsp_links",
            "_set_hardware_sink_mute",
        )
        for name in (
            "pause_playback",
            "api_spotify_pause",
            "api_spotify_next",
            "api_spotify_previous",
        ):
            body = function_source(name)
            for symbol in forbidden:
                self.assertNotIn(symbol, body, f"{name}: {symbol}")

    def test_spotify_toggle_only_coordinator_handoffs_when_starting(self):
        body = function_source("api_spotify_toggle")
        self.assertIn('sd.get("status") == "Playing"', body)
        self.assertIn("spotify_pause", body)
        self.assertIn("_run_coordinated_transition", body)

    def test_measurement_release_has_single_coordinator_owned_playback_restore(self):
        body = function_source("_release")
        self.assertIn("playback_transition_coordinator.restore_measurement", body)
        self.assertIn("playback_restore_via_coordinator", body)
        self.assertIn("direct restore is intentionally suppressed", body)
        self.assertIn("measurement_only_restore", body)

    def test_watchers_only_request_coordinator_recovery(self):
        # The link watcher lives in dsp/orchestration.py; it must request
        # Coordinator recovery through the injected dep and must never
        # re-sync the runtime or reclean the graph directly.
        watcher = function_source("runtime_link_watch_loop")
        self.assertIn("self._deps.request_coordinated_recovery", watcher)
        self.assertNotIn("sync_runtime", watcher)
        self.assertNotIn("_reclean_guarded", watcher)

    def test_coordinator_module_exists_and_owns_gate_state(self):
        coordinator = (ROOT / "playback" / "transition" / "coordinator.py").read_text()
        models = (ROOT / "playback" / "transition" / "models.py").read_text()
        self.assertIn("class PlaybackTransitionCoordinator", coordinator)
        self.assertIn("class OutputGateState", models)
        self.assertIn("failure_latched", coordinator)
        self.assertIn("output-gate-restore", coordinator)

    def test_playback_orchestration_owns_graph_and_rate_implementations(self):
        orchestration = SOURCES["playback/orchestration.py"]
        for name in (
            "transition_sample_rate_policy", "playback_graph_diagnosis",
            "reconcile_post_start_graph", "establish_effects_and_helper",
            "relink_missing_production_links",
        ):
            self.assertIn(f"def {name}", orchestration)
        main = SOURCES["main.py"]
        self.assertNotIn("async def run_pw_link_command", main)
        self.assertIn("self._deps.run_pw_link_command", orchestration)
        self.assertNotIn("Build the effects/helper graph inside the Coordinator-owned gate", main)
        self.assertNotIn("def _ensure_mpv_to_dsp_links", main)
        self.assertIn("def _ensure_mpv_to_dsp_links", orchestration)

    def test_status_never_commits_playing_while_transition_is_active(self):
        body = function_source("build_playback_payload")
        self.assertIn('transition_status.get("active")', body)
        self.assertIn('"transitioning"', body)


if __name__ == "__main__":
    unittest.main()
