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

    def test_active_playback_entrypoints_do_not_prearm_or_bypass_coordinator(self):
        for name in (
            "play_track",
            "toggle_playback",
            "_load_queue_track",
            "pause_playback",
            "api_spotify_play",
            "api_spotify_pause",
            "api_spotify_toggle",
            "api_spotify_next",
            "api_spotify_previous",
        ):
            body = function_source(name)
            self.assertNotIn("_prearm_known_local_samplerate", body, name)
            self.assertNotIn("_prearm_spotify_samplerate", body, name)
            self.assertIn("_run_coordinated_transition", body, name)

    def test_measurement_release_calls_coordinator_before_legacy_restore_paths(self):
        body = function_source("_release")
        self.assertIn("playback_transition_coordinator.restore_measurement", body)
        self.assertIn("coordinator_attempted", body)
        self.assertIn("not coordinator_attempted", body)

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


if __name__ == "__main__":
    unittest.main()
