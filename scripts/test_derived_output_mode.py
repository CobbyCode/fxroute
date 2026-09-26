#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Derived overview mode payload: v2 head translates to the legacy shape.

The v2 output state is the single source of truth; the overview builder
derives its legacy ``output_mode`` payload (mode label, subwoofer blocks,
crossover fields) from the committed head instead of the removed mode
files. Unusable heads (None, garbage, failing loader) fall back to the
file path so behavior degrades exactly like a missing mode file.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_state import (default_bass_management, default_output_state,
                                set_crossover, set_mode_routing, switch_mode)
from audio.samplerate import OUTPUT_MODE_SUBWOOFER_22_MODES, OUTPUT_MODE_SUBWOOFER_MODES
from audio.samplerate import overview as overview_module
from audio.samplerate.overview import (
    get_audio_output_overview,
    _derived_output_mode,
    _derived_output_mode_from_head,
    configure_output_state_head,
)
from dsp.runtime import BassManagementConfig


def head_with(mode, key, roles, *, frequency_hz=80, highpass=True, subs=None,
              family=None, slope_db_oct=None, sub_link=None, sub_filters=None):
    state = switch_mode(set_mode_routing(
        default_output_state(), mode, key, ["main_l", "main_r", *roles]), mode)
    spec = state["modes"][mode]
    spec["bass_management"] = {**default_bass_management(),
                               "frequency_hz": frequency_hz,
                               "main_highpass_enabled": highpass}
    for key, value in (("family", family), ("slope_db_oct", slope_db_oct),
                       ("sub_link", sub_link), ("sub_filters", sub_filters)):
        if value is not None:
            spec["bass_management"][key] = value
    for role, settings in (subs or {}).items():
        spec["processing"][role].update(settings)
    return state


def crossover_head_with(mode, key, roles, *, frequency_hz=80, highpass=True, subs=None):
    """Head whose routing uses the crossover way vocabulary.

    Built with the production mutations only: enabling the crossover renames
    the main ports to ``left_low``/``right_low``, then the requested way and
    sub roles are routed. This is the vocabulary the bug lived in — with the
    crossover on, the mains are named ``left_*``/``right_*``, so a name filter
    or a positional pick mistakes them for subs.
    """
    state = set_crossover(default_output_state(), mode, True)
    state = set_mode_routing(state, mode, key, roles)
    state = switch_mode(state, mode)
    spec = state["modes"][mode]
    spec["bass_management"] = {**default_bass_management(),
                               "frequency_hz": frequency_hz,
                               "main_highpass_enabled": highpass}
    for role, settings in (subs or {}).items():
        # Only routed roles own a processing entry, as set_mode_routing seeds
        # them; the full map can be passed regardless.
        entry = spec["processing"].get(role)
        if isinstance(entry, dict):
            entry.update(settings)
    return state


# Distinct per-role delay/level so a main way can never be mistaken for a sub.
XO_PROCESSING = {
    "left_high": {"alignment_ms": 6.91, "level_db": -4.5, "polarity": "invert"},
    "left_low": {"alignment_ms": 0.0, "level_db": 0.0, "polarity": "normal"},
    "right_high": {"alignment_ms": 0.46, "level_db": 1.48, "polarity": "normal"},
    "right_low": {"alignment_ms": 0.0, "level_db": -0.5, "polarity": "normal"},
    "sub1": {"alignment_ms": 0.06, "level_db": 2.7, "polarity": "normal"},
    "sub2": {"alignment_ms": 0.52, "level_db": 3.7, "polarity": "invert"},
    "sub_l": {"alignment_ms": 7.49, "level_db": 0.5, "polarity": "normal"},
    "sub_r": {"alignment_ms": 1.43, "level_db": -0.5, "polarity": "invert"},
}
XO_WAYS = ["left_low", "left_high", "right_low", "right_high"]


class DerivedModeTests(unittest.TestCase):
    def test_stereo_head_reads_stereo(self):
        payload = _derived_output_mode(head_with("stereo", "dev", []), "dev")
        self.assertEqual(payload["mode"], "stereo")
        self.assertEqual(payload["subwoofer"]["crossover_frequency_hz"], 80)

    def test_single_sub_reads_21_with_settings(self):
        head = head_with("stereo-sub", "dev", ["sub1"], frequency_hz=150,
                         highpass=False,
                         subs={"sub1": {"level_db": -3.0, "alignment_ms": 1.2,
                                        "polarity": "invert"}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.1")
        sub = payload["subwoofer"]
        self.assertEqual(sub["crossover_frequency_hz"], 150)
        self.assertIs(sub["main_highpass_enabled"], False)
        self.assertEqual(sub["sub_level_db"], -3.0)
        self.assertEqual(sub["sub_alignment_ms"], 1.2)
        self.assertEqual(sub["sub_polarity"], "invert")

    def test_dual_sub_reads_22_and_keeps_minus_80_park(self):
        head = head_with("stereo-sub", "dev", ["sub1", "sub2"],
                         subs={"sub1": {"level_db": 1.5, "alignment_ms": 1.0,
                                        "polarity": "invert"},
                               "sub2": {"level_db": -80.0, "alignment_ms": 3.0,
                                        "polarity": "normal"}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2")
        self.assertEqual(payload["crossover_frequency_hz"], 80)
        self.assertEqual(payload["slope"], "LR24")
        self.assertEqual(payload["subwoofers"]["sub1"]["level_db"], 1.5)
        self.assertEqual(payload["subwoofers"]["sub1"]["polarity"], "invert")
        self.assertEqual(payload["subwoofers"]["sub2"]["level_db"], -80.0)
        self.assertEqual(payload["subwoofers"]["sub2"]["alignment_ms"], 3.0)

    def test_stereo_subs_read_22_stereo_label(self):
        head = head_with("stereo-sub", "dev", ["sub_l", "sub_r"])
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2-stereo")

    def test_crossover_type_and_slope_are_reported(self):
        head = head_with("stereo-sub", "dev", ["sub1"], frequency_hz=90,
                         family="butterworth", slope_db_oct=36)
        sub = _derived_output_mode(head, "dev")["subwoofer"]
        self.assertEqual(sub["crossover_frequency_hz"], 90)
        self.assertEqual(sub["slope"], "BW36")

    def test_unlinked_stereo_pair_reports_its_own_side_crossovers(self):
        head = head_with("stereo-sub", "dev", ["sub_l", "sub_r"],
                         sub_link=False,
                         sub_filters={
                             "left": {"family": "bessel", "slope_db_oct": 18, "frequency_hz": 60},
                             "right": {"family": "butterworth", "slope_db_oct": 24, "frequency_hz": 120}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["crossover_frequency_hz"], 60)
        self.assertEqual(payload["slope"], "BS18")
        self.assertEqual(payload["subwoofers"]["sub1"]["crossover_frequency_hz"], 60)
        self.assertEqual(payload["subwoofers"]["sub1"]["slope"], "BS18")
        self.assertEqual(payload["subwoofers"]["sub2"]["crossover_frequency_hz"], 120)
        self.assertEqual(payload["subwoofers"]["sub2"]["slope"], "BW24")

    def test_dual_mono_keeps_the_shared_crossover_for_both_slots(self):
        head = head_with("stereo-sub", "dev", ["sub1", "sub2"], frequency_hz=70,
                         sub_link=False,
                         sub_filters={"left": {"family": "bessel", "slope_db_oct": 12,
                                                "frequency_hz": 60}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2")
        self.assertEqual(payload["crossover_frequency_hz"], 70)
        self.assertEqual(payload["subwoofers"]["sub1"]["crossover_frequency_hz"], 70)
        self.assertEqual(payload["subwoofers"]["sub2"]["crossover_frequency_hz"], 70)

    def test_unusable_heads_fall_back(self):
        self.assertIsNone(_derived_output_mode(None, "dev"))
        self.assertIsNone(_derived_output_mode({"no": "modes"}, "dev"))
        self.assertIsNone(_derived_output_mode({"active_mode": "stereo-sub",
                                                "modes": {}}, "dev"))
        self.assertIsNone(_derived_output_mode(
            head_with("stereo-sub", "dev", ["sub1"]), "other-key"))

    def test_parity_with_legacy_file_content(self):
        """Same settings via file and via v2 head must agree on key fields."""
        legacy = {
            "mode": "subwoofer-2.1",
            "subwoofer": {"crossover_frequency_hz": 150,
                          "main_highpass_enabled": False,
                          "sub_level_db": -3.0, "sub_alignment_ms": 1.2,
                          "sub_polarity": "invert"},
        }
        head = head_with("stereo-sub", "dev", ["sub1"], frequency_hz=150,
                         highpass=False,
                         subs={"sub1": {"level_db": -3.0, "alignment_ms": 1.2,
                                        "polarity": "invert"}})
        derived = _derived_output_mode(head, "dev")["subwoofer"]
        for key, value in legacy["subwoofer"].items():
            self.assertEqual(derived[key], value, key)


class CrossoverDerivedModeTests(unittest.TestCase):
    """With the crossover on, only real sub roles may reach the sub slots.

    Regression for the derived overview scanning the assignment list itself:
    the mains are named ``left_*``/``right_*`` there, so they passed the name
    filter, and ``subs[0]/subs[1]`` then reported the alphabetically first two
    main ways as sub1/sub2 — which also made every crossover-enabled system
    read as 2.2 because the ``len(subs) == 1`` test could never be reached.
    """

    def test_crossover_without_subs_reads_stereo(self):
        head = crossover_head_with("stereo-sub", "dev", [*XO_WAYS], subs=XO_PROCESSING)
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "stereo")
        self.assertNotIn("subwoofers", payload)
        # The measurement-save context and the 2.2 ER tolerance both gate on
        # this label, so a sub-free system must not claim a subwoofer mode.
        self.assertNotIn(payload["mode"], OUTPUT_MODE_SUBWOOFER_MODES)

    def test_crossover_single_sub_reads_21_with_the_sub_values(self):
        head = crossover_head_with("stereo-sub", "dev",
                                   [*XO_WAYS, "sub1"], subs=XO_PROCESSING)
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.1")
        self.assertNotIn("subwoofers", payload)
        sub = payload["subwoofer"]
        self.assertEqual(sub["sub_alignment_ms"], 0.06)
        self.assertEqual(sub["sub_level_db"], 2.7)
        self.assertEqual(sub["sub_polarity"], "normal")
        # left_high would have supplied 6.91 ms / -4.5 dB before the fix.
        self.assertNotEqual(sub["sub_alignment_ms"], 6.91)

    def test_crossover_dual_sub_reads_22_with_the_sub_values(self):
        # The .104 shape: sub roles interleaved between the main ways.
        head = crossover_head_with(
            "stereo-sub", "dev",
            ["left_low", "right_low", "sub1", "sub2", "left_high", "right_high"],
            subs=XO_PROCESSING)
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2")
        sub1, sub2 = payload["subwoofers"]["sub1"], payload["subwoofers"]["sub2"]
        self.assertEqual((sub1["alignment_ms"], sub1["level_db"], sub1["polarity"]),
                         (0.06, 2.7, "normal"))
        self.assertEqual((sub2["alignment_ms"], sub2["level_db"], sub2["polarity"]),
                         (0.52, 3.7, "invert"))

    def test_crossover_stereo_subs_read_22_stereo_with_the_sub_values(self):
        head = crossover_head_with("stereo-sub", "dev",
                                   [*XO_WAYS, "sub_l", "sub_r"], subs=XO_PROCESSING)
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2-stereo")
        sub1, sub2 = payload["subwoofers"]["sub1"], payload["subwoofers"]["sub2"]
        self.assertEqual((sub1["alignment_ms"], sub1["level_db"], sub1["polarity"]),
                         (7.49, 0.5, "normal"))
        self.assertEqual((sub2["alignment_ms"], sub2["level_db"], sub2["polarity"]),
                         (1.43, -0.5, "invert"))

    def test_crossover_sub_slots_survive_every_role_order(self):
        # The slot mapping is by role, not by position in the assignment list.
        orders = (
            ["left_low", "right_low", "sub1", "sub2", "left_high", "right_high"],
            ["sub1", "sub2", "left_low", "left_high", "right_low", "right_high"],
            ["left_high", "right_high", "sub2", "sub1", "right_low", "left_low"],
            ["right_low", "sub1", "left_low", "right_high", "sub2", "left_high"],
        )
        for order in orders:
            with self.subTest(order=order):
                payload = _derived_output_mode(
                    crossover_head_with("stereo-sub", "dev", order, subs=XO_PROCESSING), "dev")
                self.assertEqual(payload["mode"], "subwoofer-2.2")
                self.assertEqual(payload["subwoofers"]["sub1"]["alignment_ms"], 0.06)
                self.assertEqual(payload["subwoofers"]["sub1"]["level_db"], 2.7)
                self.assertEqual(payload["subwoofers"]["sub2"]["alignment_ms"], 0.52)
                self.assertEqual(payload["subwoofers"]["sub2"]["level_db"], 3.7)

    def test_derived_delay_projection_matches_the_sub_roles(self):
        # The values /api/audio/outputs publishes as derived_*_delay_ms come
        # from BassManagementConfig, which reads this payload.
        head = crossover_head_with(
            "stereo-sub", "dev",
            ["left_low", "right_low", "sub1", "sub2", "left_high", "right_high"],
            subs=XO_PROCESSING)
        overview = {"output_mode": _derived_output_mode(head, "dev")}
        config = BassManagementConfig.from_overview(overview)
        self.assertEqual(config.sub_alignment_ms, 0.06)
        self.assertEqual(config.sub2_alignment_ms, 0.52)
        self.assertEqual(config.sub_level_db, 2.7)
        self.assertEqual(config.sub2_level_db, 3.7)
        # Neither sub alignment is negative, so no main offset is introduced.
        self.assertEqual(config.derived_main_delay_ms, 0.0)
        self.assertEqual(config.derived_sub1_delay_ms, 0.06)
        self.assertEqual(config.derived_sub2_delay_ms, 0.52)

    def test_crossover_single_sub_projection_stays_21(self):
        head = crossover_head_with("stereo-sub", "dev",
                                   [*XO_WAYS, "sub1"], subs=XO_PROCESSING)
        overview = {"output_mode": _derived_output_mode(head, "dev")}
        self.assertNotIn(overview["output_mode"]["mode"], OUTPUT_MODE_SUBWOOFER_22_MODES)
        config = BassManagementConfig.from_overview(overview)
        self.assertEqual(config.sub_alignment_ms, 0.06)
        self.assertEqual(config.derived_sub_delay_ms, 0.06)

    def _three_sub_head(self):
        state = set_crossover(default_output_state(), "stereo-sub", True)
        head = set_mode_routing(
            state, "stereo-sub", "dev", [*XO_WAYS, "sub1", "sub2", "sub_l"])
        head = switch_mode(head, "stereo-sub")
        processing = head["modes"]["stereo-sub"]["processing"]
        for role, settings in XO_PROCESSING.items():
            if isinstance(processing.get(role), dict):
                processing[role].update(settings)
        return head

    def test_unsupported_config_yields_neutral_projection_values(self):
        # The peak-safety projection must not receive a main way's delay/level
        # as a sub value: mode is None, so it falls to the neutral default.
        payload = _derived_output_mode(self._three_sub_head(), "dev")
        config = BassManagementConfig.from_overview(
            {"output_mode": payload,
             "selected_output": {"key": "dev", "channels": 8, "active_rate": 48_000}})
        self.assertNotIn(config.output_mode, OUTPUT_MODE_SUBWOOFER_MODES)
        self.assertEqual(config.sub_alignment_ms, 0.0)
        self.assertEqual(config.sub2_alignment_ms, 0.0)
        self.assertEqual(config.derived_sub1_delay_ms, 0.0)
        self.assertEqual(config.derived_sub2_delay_ms, 0.0)

    def test_more_than_two_sub_roles_claim_no_mode(self):
        # Stereo would be a false claim here, and an invented sub1/sub2 pair
        # would feed wrong values to the measurement context and the peak
        # projection, so the payload claims no mode and names the roles.
        payload = _derived_output_mode(self._three_sub_head(), "dev")
        self.assertIsNone(payload["mode"])
        self.assertNotIn(payload["mode"], OUTPUT_MODE_SUBWOOFER_MODES)
        self.assertNotIn("subwoofers", payload)
        self.assertEqual(sorted(payload["unsupported_sub_roles"]),
                         ["sub1", "sub2", "sub_l"])
        self.assertIn("At most two distinct sub roles", payload["unsupported_reason"])

    def _overview_for(self, head):
        """Build the real overview payload around a head, with the host reads stubbed."""
        def run(command):
            if command[:4] == ["pactl", "list", "sinks", "short"]:
                return "1\tdev\tPipeWire\ts16le 8ch 48000Hz\tRUNNING\n"
            return ""

        status = {"available": True, "sink": {"name": "dev"},
                  "relevant_sink": {"name": "dev"}, "notes": []}
        with mock.patch.object(overview_module, "_run_command", side_effect=run), \
                mock.patch.object(overview_module, "get_bluetooth_audio_overview",
                                  return_value={"available": False}), \
                mock.patch.object(overview_module, "_output_state_head_loader",
                                  return_value=head):
            return get_audio_output_overview(status)

    def test_unsupported_sub_config_is_surfaced_as_an_overview_note(self):
        overview = self._overview_for(self._three_sub_head())
        self.assertIsNone(overview["output_mode"]["mode"])
        self.assertNotIn("subwoofers", overview["output_mode"])
        notes = [note for note in overview["notes"] if "cannot be represented" in note]
        self.assertEqual(len(notes), 1, overview["notes"])
        for role in ("sub1", "sub2", "sub_l"):
            self.assertIn(role, notes[0])

    def test_supported_sub_configs_report_their_mode_and_add_no_such_note(self):
        for roles, expected in ((XO_WAYS, "stereo"),
                                ([*XO_WAYS, "sub1"], "subwoofer-2.1"),
                                ([*XO_WAYS, "sub1", "sub2"], "subwoofer-2.2"),
                                ([*XO_WAYS, "sub_l", "sub_r"], "subwoofer-2.2-stereo")):
            with self.subTest(mode=expected):
                head = crossover_head_with("stereo-sub", "dev", roles, subs=XO_PROCESSING)
                overview = self._overview_for(head)
                self.assertEqual(overview["output_mode"]["mode"], expected)
                self.assertFalse([n for n in overview["notes"] if "cannot be represented" in n])

    def test_crossover_disabled_path_is_unchanged(self):
        # The main_l/main_r vocabulary never leaked and must keep working.
        payload = _derived_output_mode(
            head_with("stereo-sub", "dev", ["sub1", "sub2"],
                      subs={"sub1": {"level_db": 1.5, "alignment_ms": 1.0},
                            "sub2": {"level_db": -80.0, "alignment_ms": 3.0}}), "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2")
        self.assertEqual(payload["subwoofers"]["sub1"]["level_db"], 1.5)
        self.assertEqual(payload["subwoofers"]["sub2"]["alignment_ms"], 3.0)


class LoaderFallbackTests(unittest.TestCase):
    def tearDown(self):
        configure_output_state_head(None)

    def test_failing_loader_falls_back(self):
        configure_output_state_head(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIsNone(
            _derived_output_mode_from_head({"selected_key": "dev"}))

    def test_missing_loader_falls_back(self):
        configure_output_state_head(None)
        self.assertIsNone(
            _derived_output_mode_from_head({"selected_key": "dev"}))

    def test_wired_loader_feeds_builder_payload(self):
        head = head_with("stereo-sub", "dev", ["sub1"])
        configure_output_state_head(lambda: head)
        payload = _derived_output_mode_from_head({"selected_key": "dev"})
        self.assertEqual(payload["mode"], "subwoofer-2.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
