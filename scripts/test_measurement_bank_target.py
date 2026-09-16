#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen measurement targets: area freeze, output masking, and stored context."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import (
    default_output_state,
    select_bank,
    set_bank_preset,
    set_mode_routing,
    switch_mode,
    validate_output_state,
)
from audio.output_state_store import OutputStateStore
from dsp.persistence import DSPPresetStore
from measurement.store import MeasurementStore
from measurement.target import (
    GLOBAL_BANK_ID,
    LEGACY_TARGET,
    REFERENCE_TAP_INGRESS,
    attach_measurement_target,
    freeze_measurement_target,
    measurement_target_from_context,
    require_commit_target,
    require_target_matches,
    summed_role_ids,
    sweep_output_masks,
    target_output_mask,
    targets_compatible,
)

CROSSOVER_ROLES = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
PRESETS = (
    ("Neutral", []),
    ("Direct", []),
    ("Room EQ", [{"id": "eq", "type": "equalizer", "params": {"bands": [
        {"filterType": "bell", "frequencyHz": 1000, "gainDb": -3, "q": 1}]}}]),
)


def crossover_filter(frequency):
    return {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": frequency}


def crossover_state(*, with_subs=True, assignments=None, ways=("low", "mid", "high")):
    """A complete, activatable Crossover state with filters on every way."""
    wiring = list(assignments) if assignments is not None else [
        f"{side}_{way}" for side in ("left", "right") for way in ways]
    if with_subs:
        wiring += ["sub1", "sub2"]
    state = switch_mode(set_mode_routing(default_output_state(), "crossover", "A", wiring), "crossover")
    for role, settings in state["modes"]["crossover"]["processing"].items():
        if role in ("sub1", "sub2"):
            continue
        if not role.endswith("low"):
            settings["highpass"] = crossover_filter(300 if "mid" in role else 2500)
        if not role.endswith("high"):
            settings["lowpass"] = crossover_filter(300 if role.endswith("low") else 2500)
    return validate_output_state(state)


def measurement_payload(measurement_id, *, channel, target=None):
    payload = {
        "id": measurement_id,
        "name": measurement_id,
        "channel": channel,
        "calibration": {"filename": "mic.txt", "applied": True},
        "traces": [{"kind": "sweep-response", "role": "trusted",
                    "points": [[20, 0], [100, 5], [1000, 10], [10000, 15]]}],
    }
    if target is not None:
        payload["measurement_target"] = target
    return payload


def analysis_payload():
    return {
        "method": "inverse-sweep",
        "sample_rate": 48000,
        "rms_dbfs": -30.0,
        "peak_dbfs": -12.0,
        "window_count": 2,
        "normalized_by_db": 0.0,
        "alignment_samples": 10,
        "alignment_seconds": 0.001,
        "trusted_min_hz": 30.0,
        "trusted_max_hz": 18000.0,
        "raw_point_count": 3,
        "review_point_count": 3,
        "display_point_count": 3,
        "trusted_band_meta": {},
        "review_band_meta": {},
        "quality_checks": {"items": []},
        "capture_audit": {},
        "clock": {},
        "impulse_response": {},
        "trusted_points": [[20, 0], [100, 5], [1000, 10]],
        "review_points": [[20, 0], [100, 5], [1000, 10]],
    }


class TargetFixture:
    """Preset library plus real output service shared by both suites."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.presets = DSPPresetStore(self.root / "presets", self.root / "irs")
        for name, chain in PRESETS:
            self.presets.write(name, {"schema": "fxroute.dsp.preset", "version": 1, "chain": chain})
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(self.root / "output-state.json"),
            preset_loader=self.presets.read,
            resolve_ir=self._no_ir,
            measurement_active=lambda: False,
        ))

    @staticmethod
    def _no_ir(kernel):
        raise AssertionError(f"no IR expected, got {kernel}")

    def fingerprint(self, state, channels=8, output_key="A", sample_rate_hz=48000):
        return self.service.fingerprint(state, output_key=output_key, channels=channels,
                                        sample_rate_hz=sample_rate_hz)

    def freeze(self, state, bank_id, *, channels=8, output_key="A", sample_rate_hz=48000):
        return freeze_measurement_target(
            state, bank_id=bank_id, output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz,
            fingerprint=self.fingerprint(state, channels=channels, output_key=output_key,
                                         sample_rate_hz=sample_rate_hz))

    @staticmethod
    def raw_freeze(state, bank_id, **overrides):
        """Freeze with an opaque fingerprint token to isolate target validation."""
        arguments = {"output_key": "A", "channels": 8, "sample_rate_hz": 48000,
                     "fingerprint": "test-fingerprint"}
        arguments.update(overrides)
        return freeze_measurement_target(state, bank_id=bank_id, **arguments)


class MeasurementTargetTests(TargetFixture, unittest.TestCase):
    def test_global_target_records_full_topology_and_masks_nothing(self):
        target = self.freeze(crossover_state(), GLOBAL_BANK_ID)
        self.assertEqual(target["schema"], "fxroute.measurement-target")
        self.assertEqual(target["version"], 1)
        self.assertEqual(target["mode"], "crossover")
        self.assertEqual(target["bank_id"], "global")
        self.assertEqual(target["preset"], "Neutral")
        self.assertEqual(target["revision"], 0)
        self.assertEqual(target["sample_rate_hz"], 48000)
        self.assertEqual(target["reference_tap"], REFERENCE_TAP_INGRESS)
        self.assertEqual(target["roles"], [*CROSSOVER_ROLES, "sub1", "sub2"])
        self.assertEqual(target["measured_roles"], target["roles"])
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 0)
        self.assertEqual(json.loads(json.dumps(target)), target)

    def test_area_target_mutes_unrelated_outputs_and_keeps_fanout(self):
        state = set_mode_routing(default_output_state(), "stereo", "A",
                                 ["sub1", "off", "main_l", "main_r", "sub1"])
        target = self.freeze(state, "sub1", channels=5)
        self.assertEqual(target["roles"], ["main_l", "main_r", "sub1"])
        self.assertEqual(target["measured_roles"], ["sub1"])
        # Both fanned-out Sub outputs stay audible; only the Main outputs mute.
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 0b011)
        left = self.freeze(state, "main_l", channels=5)
        self.assertEqual(target_output_mask(left, roles=left["roles"]), 0b110)

    def test_freeze_rejects_unrouted_unknown_incomplete_and_invalid_targets(self):
        stereo = default_output_state()
        with self.assertRaisesRegex(ValueError, "not stored in output mode stereo"):
            self.raw_freeze(stereo, "sub1")
        with self.assertRaisesRegex(ValueError, "not stored in output mode stereo"):
            self.raw_freeze(stereo, "global_extra")
        incomplete = switch_mode(
            set_mode_routing(default_output_state(), "crossover", "A", ["left_low", "right_low"]), "crossover")
        with self.assertRaisesRegex(ValueError, "complete Low/High"):
            self.raw_freeze(incomplete, "global")
        state = crossover_state()
        for rate in (0, -48000, 400000, True):
            with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, "sample rate"):
                self.raw_freeze(state, "left_mid", sample_rate_hz=rate)
        for channels in (0, 33, True):
            with self.subTest(channels=channels), self.assertRaisesRegex(ValueError, "channel count"):
                self.raw_freeze(state, "left_mid", channels=channels)
        for fingerprint in ("", "   ", "abc def"):
            with self.subTest(fingerprint=fingerprint), self.assertRaisesRegex(ValueError, "fingerprint"):
                self.raw_freeze(state, "left_mid", fingerprint=fingerprint)
        with self.assertRaisesRegex(ValueError, "device key"):
            self.raw_freeze(state, "left_mid", output_key="   ")
        # A dormant role keeps its bank but is not a measureable area.
        four_way = crossover_state(ways=("low", "low_mid", "mid", "high"))
        dormant = set_mode_routing(four_way, "crossover", "A", CROSSOVER_ROLES)
        self.assertIn("left_low_mid", dormant["modes"]["crossover"]["banks"])
        with self.assertRaisesRegex(ValueError, "not an active role"):
            self.freeze(dormant, "left_low_mid")

    def test_target_is_detached_from_later_state_edits(self):
        state = crossover_state()
        target = self.freeze(state, "left_mid")
        state["revision"] = 9
        state["modes"]["crossover"]["banks"]["left_mid"]["preset"] = "Room EQ"
        state["modes"]["crossover"]["selected_bank"] = "left_high"
        self.assertEqual(target["revision"], 0)
        self.assertEqual(target["preset"], "Neutral")
        self.assertEqual(target["bank_id"], "left_mid")
        self.assertEqual(target["processing_fingerprint"], self.fingerprint(crossover_state(), channels=8))

    def test_editing_selection_keeps_the_target_but_processing_edits_break_it(self):
        state = crossover_state()
        frozen = self.freeze(state, "left_mid")
        selected = select_bank(state, "crossover", "A", 8, "left_mid")
        live = self.fingerprint(selected)
        self.assertEqual(frozen["processing_fingerprint"], live)
        require_target_matches(frozen, mode="crossover", bank_id="left_mid",
                               processing_fingerprint=live, output_key="A", sample_rate_hz=48000)
        edited = set_bank_preset(selected, "crossover", "left_mid", preset="Room EQ")
        with self.assertRaisesRegex(ValueError, "processing fingerprint changed"):
            require_target_matches(frozen, mode="crossover", bank_id="left_mid",
                                   processing_fingerprint=self.fingerprint(edited))
        with self.assertRaisesRegex(ValueError, "bank 'left_mid' != 'left_high'"):
            require_target_matches(frozen, mode="crossover", bank_id="left_high",
                                   processing_fingerprint=live)
        with self.assertRaisesRegex(ValueError, "mode 'crossover' != 'stereo'"):
            require_target_matches(frozen, mode="stereo", bank_id="left_mid",
                                   processing_fingerprint=live)
        with self.assertRaisesRegex(ValueError, "output device changed"):
            require_target_matches(frozen, mode="crossover", bank_id="left_mid",
                                   processing_fingerprint=live, output_key="B")
        with self.assertRaisesRegex(ValueError, "sample rate 48000 != 96000"):
            require_target_matches(frozen, mode="crossover", bank_id="left_mid",
                                   processing_fingerprint=live, sample_rate_hz=96000)
        # Legacy results carry no context to compare against.
        require_target_matches(copy.deepcopy(LEGACY_TARGET), mode="stereo", bank_id="global",
                               processing_fingerprint="unrelated")

    def test_merge_compatibility_rejects_mixed_areas_and_accepts_revision_drift(self):
        state = crossover_state()
        mid = self.freeze(state, "left_mid")
        bumped = copy.deepcopy(state)
        bumped["revision"] = 7
        self.assertTrue(targets_compatible(mid, self.freeze(bumped, "left_mid")))
        self.assertEqual(mid["revision"], 0)
        self.assertTrue(targets_compatible(mid, copy.deepcopy(mid)))
        self.assertFalse(targets_compatible(mid, self.freeze(state, "left_high")))
        self.assertFalse(targets_compatible(mid, self.freeze(state, GLOBAL_BANK_ID)))
        edited = set_bank_preset(state, "crossover", "left_mid", preset="Room EQ")
        self.assertFalse(targets_compatible(mid, self.freeze(edited, "left_mid")))
        second_device = set_mode_routing(state, "crossover", "B", [*CROSSOVER_ROLES, "sub1", "sub2"])
        other = self.freeze(second_device, "left_mid", output_key="B")
        self.assertNotEqual(mid["device_key"], other["device_key"])
        self.assertFalse(targets_compatible(mid, other))
        self.assertFalse(targets_compatible(mid, self.freeze(state, "left_mid", sample_rate_hz=96000)))
        self.assertTrue(targets_compatible(copy.deepcopy(LEGACY_TARGET), dict(LEGACY_TARGET)))
        self.assertFalse(targets_compatible(mid, LEGACY_TARGET))
        self.assertFalse(targets_compatible(None, mid))

    def test_output_mask_requires_the_measured_roles_to_still_be_routed(self):
        target = self.freeze(crossover_state(), "left_mid")
        with self.assertRaisesRegex(ValueError, "no longer routed: left_mid"):
            target_output_mask(target, roles=["left_low", "right_low", "right_high"])
        with self.assertRaisesRegex(ValueError, "unique"):
            target_output_mask(target, roles=["left_mid", "left_mid"])
        with self.assertRaisesRegex(ValueError, "legacy"):
            target_output_mask(copy.deepcopy(LEGACY_TARGET), roles=["main_l"])
        with self.assertRaisesRegex(ValueError, "exceed 32"):
            target_output_mask(target, roles=[f"role_{index}" for index in range(33)])

    def test_area_context_round_trip_and_explicit_legacy_marker(self):
        target = self.freeze(crossover_state(), "left_mid")
        context = attach_measurement_target({"output_mode": "crossover", "output_key": "A"}, target)
        self.assertEqual(context["output_mode"], "crossover")
        self.assertEqual(context["measurement_target"], target)
        self.assertEqual(measurement_target_from_context(context), target)
        self.assertEqual(measurement_target_from_context(None), LEGACY_TARGET)
        self.assertEqual(measurement_target_from_context({}), LEGACY_TARGET)
        self.assertEqual(measurement_target_from_context({"measurement_target": {"schema": "other"}}), LEGACY_TARGET)
        self.assertEqual(attach_measurement_target(None, target), {"measurement_target": target})
        context["measurement_target"]["bank_id"] = "mutated"
        self.assertEqual(target["bank_id"], "left_mid")
        self.assertEqual(measurement_target_from_context(context)["bank_id"], "mutated")
        with self.assertRaisesRegex(ValueError, "frozen target"):
            attach_measurement_target({}, {"bank_id": "global"})


class StoredAreaContextTests(TargetFixture, unittest.TestCase):
    """Area context survives capture, persistence, export and merging."""

    def setUp(self):
        super().setUp()
        self.environment = patch.dict("os.environ", {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = MeasurementStore(home=self.root)

    def test_capture_result_and_saved_measurement_keep_the_frozen_target(self):
        target = self.freeze(crossover_state(), "left_mid")
        measurement = self.store._persistence._build_measurement_from_analysis(
            analysis_payload(),
            input_device={"id": "mic", "label": "Mic"},
            channel="left",
            calibration={"applied": False},
            measurement_target=target,
        )
        self.assertEqual(measurement["measurement_target"], target)
        saved = self.store.save_measurement(measurement)
        self.assertEqual(saved["measurement_target"], target)
        reloaded = next(item for item in self.store.list_measurements()["measurements"]
                        if item["id"] == saved["id"])
        self.assertEqual(measurement_target_from_context(reloaded), target)

    def test_merge_rejects_mixed_areas_and_accepts_revision_only_drift(self):
        mid = self.freeze(crossover_state(), "left_mid")
        high = self.freeze(crossover_state(), "left_high")
        for measurement_id, target in (("mid-a", mid), ("high-a", high)):
            self.store.save_measurement(measurement_payload(measurement_id, channel="left", target=target))
        with self.assertRaisesRegex(ValueError, "different areas or processing"):
            self.store.merge_measurements(["mid-a", "high-a"], "Mixed")

        same_area = copy.deepcopy(mid)
        same_area["revision"] = mid["revision"] + 3
        self.store.save_measurement(measurement_payload("mid-b", channel="left", target=same_area))
        merged = self.store.merge_measurements(["mid-a", "mid-b"], "Merged mid")
        self.assertEqual(merged["measurement_target"], mid)

    def test_legacy_measurements_without_a_target_still_merge(self):
        for measurement_id in ("legacy-a", "legacy-b"):
            self.store.save_measurement(measurement_payload(measurement_id, channel="left"))
        merged = self.store.merge_measurements(["legacy-a", "legacy-b"], "Legacy merge")
        self.assertNotIn("measurement_target", merged)


class CommitTargetTests(TargetFixture, unittest.TestCase):
    """A generated correction may only be committed where it was measured."""

    def setUp(self):
        super().setUp()
        # A MeasurementStore resolves its roots from XDG, so the sandbox has to
        # be pinned before the store exists (never the developer's real library).
        self.environment = patch.dict("os.environ", {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = MeasurementStore(home=self.root)

    def test_matching_area_and_processing_is_accepted(self):
        target = self.freeze(crossover_state(), "left_mid")
        require_commit_target(target, copy.deepcopy(target), mode="crossover", bank_id="left_mid")

    def test_commit_into_another_area_is_rejected(self):
        target = self.freeze(crossover_state(), "left_mid")
        live = self.freeze(crossover_state(), "left_high")
        with self.assertRaisesRegex(ValueError, "captured for crossover area 'left_mid'"):
            require_commit_target(target, live, mode="crossover", bank_id="left_high")
        with self.assertRaisesRegex(ValueError, "captured for crossover area 'left_mid'"):
            require_commit_target(target, copy.deepcopy(target), mode="crossover", bank_id="left_high")

    def test_processing_and_device_changes_are_rejected_with_details(self):
        target = self.freeze(crossover_state(), "left_mid")
        for field, value, label in (
            ("processing_fingerprint", "other-fingerprint", "processing"),
            ("device_key", "device-b", "output device"),
            ("sample_rate_hz", 44100, "sample rate"),
            ("reference_tap", "later", "reference tap"),
            ("measured_roles", ["left_high"], "measured roles"),
        ):
            live = copy.deepcopy(target)
            live[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, label):
                require_commit_target(target, live, mode="crossover", bank_id="left_mid")

    def test_legacy_and_unavailable_contexts(self):
        # A measurement from before the frozen-target era stays committable.
        require_commit_target(LEGACY_TARGET, {}, mode="crossover", bank_id="left_mid")
        with self.assertRaisesRegex(ValueError, "no frozen target"):
            require_commit_target({}, {}, mode="crossover", bank_id="left_mid")
        target = self.freeze(crossover_state(), "left_mid")
        for live in ({}, LEGACY_TARGET, None):
            with self.subTest(live=live), self.assertRaisesRegex(ValueError, "unavailable"):
                require_commit_target(target, live, mode="crossover", bank_id="left_mid")

    def test_stored_measurement_lookup_keeps_its_target(self):
        target = self.freeze(crossover_state(), "left_mid")
        self.store.save_measurement(measurement_payload("saved-mid", channel="left", target=target))
        stored = self.store.get_measurement("saved-mid")
        self.assertEqual(stored["measurement_target"], target)
        require_commit_target(stored["measurement_target"], copy.deepcopy(target),
                              mode="crossover", bank_id="left_mid")
        with self.assertRaises(KeyError):
            self.store.get_measurement("missing")
        with self.assertRaisesRegex(ValueError, "Invalid measurement id"):
            self.store.get_measurement("../escape")


class SweepOutputMaskTests(TargetFixture, unittest.TestCase):
    """Internal way sweeps of a two-sided capture over one frozen area."""

    def test_global_repeat_masks_the_other_side_per_internal_sweep(self):
        state = set_mode_routing(default_output_state(), "stereo", "A",
                                 ["main_l", "main_r", "sub_l", "sub_r"])
        target = self.freeze(state, GLOBAL_BANK_ID, channels=4)
        roles = target["roles"]
        self.assertEqual(roles, ["main_l", "main_r", "sub_l", "sub_r"])
        masks = sweep_output_masks(target, roles=roles)
        # Left sweep: Right Main and Right Sub are muted, Left stays audible.
        self.assertEqual(masks, {"left": 0b1010, "right": 0b0101})
        self.assertEqual(target_output_mask(target, roles=roles), 0)

    def test_mono_roles_stay_audible_in_both_sweeps(self):
        state = set_mode_routing(default_output_state(), "stereo", "A",
                                 ["main_l", "main_r", "sub1", "off"])
        target = self.freeze(state, GLOBAL_BANK_ID, channels=4)
        roles = target["roles"]
        self.assertEqual(roles, ["main_l", "main_r", "sub1"])
        # A mono sub sums both inputs, so it is part of both internal sweeps.
        self.assertEqual(sweep_output_masks(target, roles=roles),
                         {"left": 0b010, "right": 0b001})

    def test_mono_sub_is_audible_in_both_sweeps_whatever_its_name(self):
        # A lone sub role is summed from both inputs (mono routing), even when
        # it is called sub_l: muting it during the right sweep would capture
        # silence where the routing still feeds it.
        state = set_mode_routing(default_output_state(), "stereo", "A",
                                 ["main_l", "main_r", "sub_l", "off"])
        target = self.freeze(state, GLOBAL_BANK_ID, channels=4)
        roles = target["roles"]
        self.assertEqual(roles, ["main_l", "main_r", "sub_l"])
        self.assertEqual(sweep_output_masks(target, roles=roles),
                         {"left": 0b010, "right": 0b001})

    def test_summed_role_ids_matches_the_plan_input_routes(self):
        for roles, expected in (
            (["main_l", "sub_l"], {"sub_l"}),
            (["main_l", "main_r", "sub1"], {"sub1"}),
            (["sub_l", "sub_r"], set()),
            (["main_l", "main_r", "sub_l", "sub_r"], set()),
            (["sub1", "sub2"], {"sub1", "sub2"}),
            (["sub_l", "sub1"], {"sub_l", "sub1"}),
            (["main_l", "main_r"], set()),
        ):
            with self.subTest(roles=roles):
                self.assertEqual(summed_role_ids(roles), expected)

    def test_one_sided_area_keeps_the_whole_area_in_both_sweeps(self):
        target = self.freeze(crossover_state(), "left_mid")
        roles = target["roles"]
        masks = sweep_output_masks(target, roles=roles)
        area_mask = target_output_mask(target, roles=roles)
        # Nothing on the right side of this area, so neither sweep silences it.
        self.assertEqual(masks, {"left": area_mask, "right": area_mask})

    def test_multi_role_area_splits_by_sweep_side(self):
        # An area whose measured roles span both sides (a multi-way area) keeps
        # each side's roles audible in its own internal sweep.
        target = self.freeze(crossover_state(), GLOBAL_BANK_ID)
        target["measured_roles"] = ["left_mid", "right_mid", "sub1"]
        roles = target["roles"]
        masks = sweep_output_masks(target, roles=roles)
        self.assertEqual([index for index in range(len(roles))
                          if masks["left"] >> index & 1],
                         [index for index, role in enumerate(roles)
                          if role not in {"left_mid", "sub1"}])
        self.assertEqual([index for index in range(len(roles))
                          if masks["right"] >> index & 1],
                         [index for index, role in enumerate(roles)
                          if role not in {"right_mid", "sub1"}])

    def test_sweep_masks_validate_the_engine_role_list(self):
        target = self.freeze(crossover_state(), GLOBAL_BANK_ID)
        with self.assertRaisesRegex(ValueError, "must be unique"):
            sweep_output_masks(target, roles=["left_low", "left_low"])
        with self.assertRaisesRegex(ValueError, "no longer routed"):
            sweep_output_masks(target, roles=["left_low"])
        with self.assertRaises(ValueError):
            sweep_output_masks(LEGACY_TARGET, roles=target["roles"])


if __name__ == "__main__":
    unittest.main()
