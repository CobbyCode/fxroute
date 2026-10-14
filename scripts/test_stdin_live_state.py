#!/usr/bin/env python3
"""STDIN acceptance uses one revisioned bank mutation and restores both slots."""

import sys
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stdin_audio_live as live
from audio.output_state import default_output_state, set_bank_preset, switch_mode
from audio.input_output_mode import remember_mode, restore_mode
from dsp.manager import DSPManager
from dsp.effects_extras import merge_effects_extras_from_json


class StdinLiveStateTests(unittest.TestCase):
    def test_setup_and_cleanup_preserve_the_stdin_output_mode_binding(self):
        state = default_output_state()
        state["input_modes"] = {"app-playback": "stereo", "stdin-input": "surround"}
        state = set_bank_preset(state, "stereo", "global",
                                preset_a="Neutral", preset_b="Direct", active_side="B")
        saved_bank = copy.deepcopy(state["modes"]["stereo"]["banks"]["global"])
        selection = {"mode": "app-playback"}
        rate_policy = {"mode": "auto", "rate": None}

        def http(method, path, payload=None):
            nonlocal state, selection, rate_policy
            if method == "GET":
                if path.endswith("/output-state"):
                    catalog = copy.deepcopy(state)
                    bank = catalog["modes"]["stereo"]["banks"]["global"]
                    bank["active_side"] = "B" if bank["preset"] == "Direct" else "A"
                    return catalog
                if path.endswith("/source-mode"):
                    return dict(selection)
                if path == "/api/status":
                    return {"volume": 27, "playing": False, "current_file": None}
                if path.endswith("/outputs"):
                    return {"selected_output": {"name": "hw", "active_rate": 44100}}
                if path.endswith("/samplerate"):
                    return {"policy": dict(rate_policy)}
                return {"extras": {}}
            if path.endswith("/source-mode"):
                target = {"mode": payload["mode"]}
                state = restore_mode(state, selection, target)
                selection = target
            elif path.endswith("/output-state/apply"):
                mutation = dict(payload["mutation"])
                kind, mode = mutation.pop("kind"), mutation.pop("mode")
                if kind == "switch_mode":
                    state = remember_mode(switch_mode(state, mode), selection)
                else:
                    state = set_bank_preset(state, mode, mutation.pop("bank_id"), **mutation)
            elif path.endswith("/samplerate"):
                rate_policy = dict(payload)
            return {"status": "ok"}

        with mock.patch.object(live, "_http", side_effect=http), \
                mock.patch.object(live.shutil, "which", return_value="/bin/test"), \
                mock.patch.object(live, "_main_dsp_outputs", return_value=("out1", "out2", "mon1", "mon2")), \
                mock.patch.object(live.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            try:
                live.StdinLiveTests.setUpClass()
                self.assertEqual(state["active_mode"], "stereo")
                self.assertEqual(state["modes"]["stereo"]["banks"]["global"]["preset"], "Neutral")
                self.assertEqual(rate_policy, {"mode": "fixed", "rate": 44100})
            finally:
                live.StdinLiveTests.doClassCleanups()
            self.assertEqual(live.StdinLiveTests.tearDown_exceptions, [])
        self.assertEqual(selection["mode"], "app-playback")
        self.assertEqual(state["active_mode"], "stereo")
        self.assertEqual(state["input_modes"], {"app-playback": "stereo", "stdin-input": "surround"})
        self.assertEqual(state["modes"]["stereo"]["banks"]["global"], saved_bank)
        self.assertEqual(rate_policy, {"mode": "auto", "rate": None})

    def test_cleanup_restores_extras_through_the_write_contract(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = DSPManager(home=Path(directory.name))
        original = manager.normalize_effects_extras({
            "headroom": {"enabled": False},
            "loudness": {"enabled": True, "params": {"strength": 4, "volumeDb": -35}}})
        current = copy.deepcopy(original)
        current["headroom"]["enabled"] = True
        current["loudness"]["params"].update(strength=10, volumeDb=-42)

        def http(method, path, payload=None):
            nonlocal current
            if method == "GET":
                return {"revision": 17, "active_mode": "stereo"}
            if path == "/api/dsp/extras":
                current = manager.normalize_effects_extras(
                    merge_effects_extras_from_json(current, payload))
            return {"status": "ok"}

        with mock.patch.multiple(live.StdinLiveTests, create=True, saved_mode="stereo",
                saved_stdin_mode=None,
                saved_bank={"preset": "Neutral", "preset_a": "Neutral", "preset_b": None,
                            "active_side": "A"}, saved_extras={"extras": original},
                saved_status={"volume": 27}, saved_source={"mode": "app-playback"}), \
                mock.patch.object(live, "_http", side_effect=http), \
                mock.patch.object(live.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            live.StdinLiveTests._restore_test_state()
        self.assertEqual(current, original)

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
                        saved_stdin_mode=None,
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
