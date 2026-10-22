#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The guard models the rendered plan its frozen target describes, and only that plan."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main
import test_output_state_lifecycle as lifecycle
import test_polarity_guard as fixtures
from audio.output_state import set_bank_preset, set_mode_routing, switch_mode
from measurement import polarity_guard as guard
from measurement.speaker_scope import plan_area

UNIT = {"roles": ["main_l"], "channel": "left", "label": "left"}


class DspPathBindingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = lifecycle.make_manager(directory.name)
        factory = mock.patch.multiple(
            main, _output_service_instance=None,
            _output_state_store_path=mock.Mock(return_value=Path(directory.name) / "output-state.json"),
            _require_dsp_manager=mock.Mock(return_value=self.manager))
        factory.start()
        self.addCleanup(factory.stop)
        self.service = main.get_output_service()
        lifecycle.seed_sub_state(self.service)
        native = SimpleNamespace(preflight_rendered=mock.AsyncMock(), fail_closed=mock.AsyncMock(),
                                 snapshot=lambda: {"active": False}, sync_rendered=mock.AsyncMock())
        context = lifecycle.lifecycle_context(self.service, self.manager, native)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def freeze(self):
        return main._freeze_measurement_target("global", 48000)

    def assess(self, target):
        path = main._resolve_polarity_dsp_path(target)
        return guard.assess_unit(path["config"], UNIT, sample_rate_hz=target["sample_rate_hz"])

    def set_extras(self, **sections):
        extras = copy.deepcopy(self.manager.load_global_extras())
        for name, update in sections.items():
            extras[name].update(update)
        self.manager.save_global_extras(extras)

    def test_the_resolved_path_is_the_one_the_sweep_ran_through(self):
        target = self.freeze()
        path = main._resolve_polarity_dsp_path(target)
        self.assertEqual([output["name"] for output in path["config"]["outputs"]], ["main_l", "main_r", "sub1"])
        self.assertEqual(path["config"]["sample_rate_hz"], 48000)
        self.assertEqual(self.assess(target)["state"], guard.OK)

    def test_extras_changed_after_the_sweep_started_make_the_path_unavailable(self):
        target = self.freeze()
        self.set_extras(loudness={"enabled": True})
        with self.assertRaises(guard.PathUnavailable) as caught:
            main._resolve_polarity_dsp_path(target)
        self.assertEqual(caught.exception.stage, "DSP path changed")

    def test_a_bank_preset_changed_after_the_sweep_started_makes_the_path_unavailable(self):
        target = self.freeze()
        revision = self.service.load()["revision"]
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="Direct"),
                           expected_revision=revision)
        with self.assertRaises(guard.PathUnavailable):
            main._resolve_polarity_dsp_path(target)

    def test_active_extras_of_the_measured_plan_make_the_unit_unknown(self):
        for section, update, stage in (("loudness", {"enabled": True}, "global loudness"),
                                       ("bass_enhancer", {"enabled": True}, "global bass enhancer"),
                                       ("tone_effect", {"enabled": True, "mode": "crystalizer"}, "global crystalizer"),
                                       ("tone_effect", {"enabled": True, "mode": "maximizer"}, "global maximizer")):
            with self.subTest(section=section, stage=stage):
                self.manager.save_global_extras(self.manager.normalize_effects_extras(None))
                self.set_extras(**{section: update})
                self.assertEqual(self.assess(self.freeze()),
                                 {"state": "unknown", "dsp_cos": None, "stage": stage})

    def test_phase_neutral_extras_leave_the_path_readable(self):
        self.set_extras(headroom={"enabled": True, "params": {"gainDb": -6}})
        self.assertEqual(self.assess(self.freeze())["state"], guard.OK)

    def test_the_direct_preset_ignores_the_extras(self):
        revision = self.service.load()["revision"]
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="Direct"),
                           expected_revision=revision)
        self.set_extras(loudness={"enabled": True})
        self.assertEqual(self.assess(self.freeze())["state"], guard.OK)

    def select_global_convolver(self, ir):
        library = self.manager.library("global", "stereo-sub")
        library.irs_dir.mkdir(parents=True, exist_ok=True)
        fixtures.write_wav(library.irs_dir / "room.wav", ir, 48000)
        library.write("RoomFIR", {"schema": "fxroute.dsp.preset", "version": 1, "chain": [
            {"id": "conv", "type": "convolver", "enabled": True,
             "params": {"kernel": "room.wav", "wet_db": 0.0, "dry_db": -100.0,
                        "input_gain_db": 0.0, "output_gain_db": 0.0}}]})
        revision = self.service.load()["revision"]
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="RoomFIR"),
                           expected_revision=revision)

    def assess_with_kernels(self, target):
        path = main._resolve_polarity_dsp_path(target)
        return guard.assess_unit(path["config"], UNIT, sample_rate_hz=target["sample_rate_hz"],
                                 kernel_path=path["kernel_path"])

    def test_a_global_fir_is_read_from_the_library_file_of_the_global_bank(self):
        self.select_global_convolver(fixtures.dirac_ir(48000, 85.0))
        self.assertEqual(self.assess_with_kernels(self.freeze())["state"], guard.OK)
        self.select_global_convolver(fixtures.phase_swing_ir(48000))
        result = self.assess_with_kernels(self.freeze())
        self.assertEqual((result["state"], result["stage"]), (guard.ROTATES, "global convolver room.wav"))

    def test_the_resolver_names_exactly_the_global_bank_file(self):
        self.select_global_convolver(fixtures.dirac_ir(48000))
        path = main._resolve_polarity_dsp_path(self.freeze())
        self.assertTrue(path["kernel_path"]("room.wav").endswith("/global/stereo-sub/room.wav"))
        with self.assertRaises(FileNotFoundError):
            path["kernel_path"]("missing.wav")

    def test_a_global_fir_without_a_resolver_cannot_be_judged(self):
        self.select_global_convolver(fixtures.dirac_ir(48000))
        self.assertEqual(self.assess(self.freeze()),
                         {"state": "unknown", "dsp_cos": None, "stage": "global convolver room.wav"})

    def test_every_unit_of_a_default_surround_setup_stays_readable(self):
        for sub in ("off", "sub1"):
            routing = ["main_l", "main_r", "center", sub, "side_l", "side_r", "rear_l", "rear_r"]
            self.service.apply(lambda state: switch_mode(set_mode_routing(state, "surround", "A", routing), "surround"),
                               expected_revision=self.service.load()["revision"])
            ports = [f"playback_AUX{index}" for index in range(len(routing))]
            main.get_audio_output_overview.return_value = {
                "selected_output": {"key": "A", "channels": len(routing)},
                "output_mode": {"effective_output_key": "A", "effective_output_channels": len(routing),
                                "hardware_playback_ports": ports}}
            for bank in ("main", "center", "side", "rear"):
                target = main._freeze_measurement_target(bank, 48000)
                path = main._resolve_polarity_dsp_path(target)
                for subject in plan_area("surround", bank, target["roles"])["units"]:
                    with self.subTest(sub=sub, bank=bank, roles=subject["roles"]):
                        result = guard.assess_unit(path["config"], subject, sample_rate_hz=48000,
                                                   kernel_path=path["kernel_path"])
                        self.assertEqual(result, {"state": "ok", "dsp_cos": 1.0, "stage": ""})

    def test_an_unreadable_output_state_is_a_failure_the_api_turns_into_unknown(self):
        target = self.freeze()
        with mock.patch.object(main, "get_audio_output_overview", side_effect=RuntimeError("no overview")):
            with self.assertRaises(RuntimeError):
                main._resolve_polarity_dsp_path(target)


if __name__ == "__main__":
    unittest.main()
