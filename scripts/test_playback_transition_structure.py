#!/usr/bin/env python3

"""Static contracts for coordinator ownership boundaries."""

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "main.py").read_text()
TREE = ast.parse(SOURCE)


def function_source(name):
    lines = SOURCE.splitlines(keepends=True)
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"function not found: {name}")


class OwnershipStructureTests(unittest.TestCase):
    def test_public_samplerate_endpoint_is_read_only(self):
        body = function_source("audio_samplerate_status")
        self.assertNotIn("_maybe_repair_active_app_samplerate_drift", body)
        self.assertNotIn("_set_pipewire_force_rate", body)
        self.assertNotIn("_sync_subwoofer_runtime", body)

    def test_coordinated_playback_entrypoints_do_not_prearm_or_bypass_coordinator(self):
        for name in (
            "play_track",
            "toggle_playback",
            "_load_queue_track",
            "api_spotify_play",
            "api_spotify_toggle",
        ):
            body = function_source(name)
            self.assertNotIn("_prearm_known_local_samplerate", body, name)
            self.assertNotIn("_prearm_spotify_samplerate", body, name)
            self.assertIn("_run_coordinated_transition", body, name)

    def test_same_source_transport_paths_do_not_enter_coordinator_or_mutate_graph(self):
        forbidden = (
            "_run_coordinated_transition",
            "_set_pipewire_force_rate",
            "_ensure_playback_samplerate_force",
            "_sync_easyeffects_preset_for_playback_samplerate",
            "_sync_subwoofer_runtime",
            "_ensure_mpv_to_easyeffects_links",
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
        watcher = function_source("_subwoofer_runtime_link_watch_loop")
        self.assertIn("_request_coordinated_recovery", watcher)
        self.assertNotIn("_sync_subwoofer_runtime(overview)", watcher)
        self.assertNotIn("_reclean_guarded", watcher)

    def test_coordinator_module_exists_and_owns_gate_state(self):
        coordinator = (ROOT / "playback_transition.py").read_text()
        self.assertIn("class PlaybackTransitionCoordinator", coordinator)
        self.assertIn("class OutputGateState", coordinator)
        self.assertIn("failure_latched", coordinator)
        self.assertIn("output-gate-restore", coordinator)

    def test_status_never_commits_playing_while_transition_is_active(self):
        body = function_source("build_playback_payload")
        self.assertIn('transition_status.get("active")', body)
        self.assertIn('"transitioning"', body)


if __name__ == "__main__":
    unittest.main()
