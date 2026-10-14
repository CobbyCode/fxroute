#!/usr/bin/env python3
"""STDIN acceptance uses one revisioned bank mutation and restores both slots."""

import sys
import copy
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stdin_audio_live as live
from audio.output_state import default_output_state, set_bank_preset


class StdinLiveStateTests(unittest.TestCase):
    def test_cleanup_restores_slotted_and_unslotted_listening(self):
        for saved in (
            {"preset": "Neutral", "preset_a": "Neutral", "preset_b": "Direct", "active_side": "A"},
            {"preset": "Direct", "preset_a": "Neutral", "preset_b": "Direct", "active_side": "B"},
            {"preset": "Direct", "preset_a": "Neutral", "preset_b": None, "active_side": None},
        ):
            with self.subTest(saved=saved):
                state = default_output_state()
                original = {key: value for key, value in saved.items() if key != "active_side"}
                state["modes"]["stereo"]["banks"]["global"] = copy.deepcopy(original)
                state = set_bank_preset(state, "stereo", "global", preset_a="Neutral",
                                        preset_b=None, active_side="A")

                def http(method, path, payload=None):
                    nonlocal state
                    if method == "GET":
                        return copy.deepcopy(state)
                    if path.endswith('/output-state/apply'):
                        mutation = dict(payload["mutation"])
                        mutation.pop("kind")
                        mode, bank = mutation.pop("mode"), mutation.pop("bank_id")
                        state = set_bank_preset(state, mode, bank, **mutation)
                    return {"status": "ok"}

                with mock.patch.multiple(live.StdinLiveTests, create=True, saved_mode="stereo",
                        saved_bank=saved, saved_extras={"extras": {}}, saved_status={"volume": 27},
                        saved_source={"mode": "app-playback"}), \
                        mock.patch.object(live, "_http", side_effect=http), \
                        mock.patch.object(live.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                    live.StdinLiveTests._restore_test_state()
                self.assertEqual(state["modes"]["stereo"]["banks"]["global"], original)

    def test_bank_mutation_reads_a_single_coherent_head(self):
        head = {"revision": 17, "active_mode": "stereo-sub"}
        calls = []

        def http(method, path, payload=None):
            calls.append((method, path, payload))
            return head if method == "GET" else {"live_applied": True}

        with mock.patch.object(live, "_http", side_effect=http):
            live._set_bank_preset("global", preset_a="Neutral", active_side="A", live_applied=True)
        self.assertEqual(calls, [
            ("GET", "/api/audio/output-state", None),
            ("POST", "/api/audio/output-state/apply", {
                "expected_revision": 17, "mutation": {"kind": "set_bank_preset",
                    "mode": "stereo-sub", "bank_id": "global",
                    "preset_a": "Neutral", "active_side": "A"}})])


if __name__ == "__main__":
    unittest.main()
