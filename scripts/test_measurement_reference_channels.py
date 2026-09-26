#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Electrical reference channels resolve the same way for store and Speaker Align.

The store resolves the reference of every take with ``resolve_reference_channels``;
the Speaker Align start check judges the same resolution before any sweep, so a
reference that the take would drop never starts a run.
"""

import asyncio
import itertools
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.reference_channels import (
    reference_candidate_channels,
    require_speaker_align_reference,
    resolve_reference_channels,
)
from measurement.store import MeasurementStore


def speaker(side, mic="1", shared="", left=None, right=None):
    return require_speaker_align_reference(
        side, mic_input_channel=mic, reference_input_channel=shared,
        reference_input_channel_left=left, reference_input_channel_right=right)


class ResolveReferenceChannelsTests(unittest.TestCase):
    def test_shared_reference_serves_both_sides(self):
        resolved = resolve_reference_channels(mic=0, shared=1, left=None, right=None)
        self.assertEqual((resolved.left, resolved.right, resolved.shared), (1, 1, True))

    def test_a_side_on_the_microphone_channel_loses_only_its_own_reference(self):
        resolved = resolve_reference_channels(mic=0, shared=None, left=0, right=7)
        self.assertEqual((resolved.left, resolved.right), (None, 7))
        self.assertEqual((resolved.collided_left, resolved.collided_right), (True, False))
        self.assertFalse(resolved.shared)

    def test_shared_reference_on_the_microphone_channel_is_gone(self):
        resolved = resolve_reference_channels(mic=0, shared=0, left=None, right=None)
        self.assertEqual((resolved.left, resolved.right), (None, None))
        self.assertTrue(resolved.collided_left and resolved.collided_right and resolved.shared)

    def test_candidates_fill_a_missing_side_and_skip_the_microphone(self):
        resolved = resolve_reference_channels(mic=0, shared=6, left=6, right=None,
                                              candidates=[6, 6, None, 0, 7])
        self.assertEqual(resolved.candidates, (6, 7))
        self.assertEqual((resolved.left, resolved.right), (6, 6))


class SpeakerAlignReferenceTests(unittest.TestCase):
    def test_reference_equal_to_the_microphone_is_refused_in_any_spelling(self):
        for options in ({"shared": "1"}, {"shared": "01"}, {"shared": " 1 "},
                        {"shared": 1}, {"mic": "", "shared": "1"}, {"mic": None, "shared": "1"},
                        {"mic": "3", "shared": "03"}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "the microphone input"):
                    speaker("left", **options)

    def test_split_reference_resolves_per_side(self):
        self.assertEqual(speaker("left", shared="7", left="7", right="8"), 7)
        self.assertEqual(speaker("right", shared="7", left="7", right="8"), 8)

    def test_a_configured_side_on_the_microphone_channel_is_refused(self):
        # The store would silently record the other side's loopback instead.
        with self.assertRaisesRegex(ValueError, "the microphone input"):
            speaker("left", left="1", right="8")
        self.assertEqual(speaker("right", left="1", right="8"), 8)

    def test_a_one_sided_reference_serves_the_other_side(self):
        self.assertEqual(speaker("left", left="7"), 7)
        self.assertEqual(speaker("right", left="7"), 7)
        self.assertEqual(speaker("left", right="8"), 8)
        self.assertEqual(speaker("right", right="8"), 8)

    def test_one_sided_reference_on_the_microphone_leaves_no_reference(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                with self.assertRaisesRegex(ValueError, "the microphone input"):
                    speaker(side, left="1")

    def test_a_stale_shared_value_on_the_microphone_does_not_block_split_references(self):
        # With per-side references set the shared field serves no side.
        self.assertEqual(speaker("left", shared="1", left="7", right="8"), 7)
        self.assertEqual(speaker("right", shared="1", left="7", right="8"), 8)

    def test_no_reference_is_refused(self):
        for options in ({}, {"shared": None}, {"shared": " ", "left": "", "right": None}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "has no electrical reference"):
                    speaker("left", **options)

    def test_shared_with_only_the_other_side_configured(self):
        # Mic 1, shared 2, left empty, right 3: the shared field serves no
        # side once a side field is set, but it is a recorded candidate, so
        # the store resolves the left side to input 2 -- decided before any
        # sweep, the same way the store decides it for every take.
        self.assertEqual(speaker("left", shared="2", left="", right="3"), 2)
        self.assertEqual(speaker("right", shared="2", left="", right="3"), 3)

    def test_error_names_the_field_to_fix(self):
        cases = (
            ({"shared": "1"}, "left", "Electrical reference input is input 1"),
            ({"left": "1", "right": "8"}, "left", "Electrical Ref L is input 1"),
            ({"left": "7", "right": "1"}, "right", "Electrical Ref R is input 1"),
            ({"right": "1"}, "left", "Electrical Ref R is input 1"),
        )
        for options, side, message in cases:
            with self.subTest(options=options, side=side):
                with self.assertRaisesRegex(ValueError, message):
                    speaker(side, **options)
        with self.assertRaisesRegex(ValueError, "Speaker Align right has no electrical reference"):
            speaker("right")

    def test_malformed_channel_is_refused(self):
        for options in ({"shared": "x"}, {"shared": "0"}, {"mic": "-1", "shared": "2"}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "input channel number"):
                    speaker("left", **options)



class StoreParityTests(unittest.TestCase):
    """The start check accepts exactly what the store resolves for the take.

    Every combination runs through the real store setup with the candidate
    list the Speaker Align takes pass. An accepted start must name the side
    reference the store records; a refused one must be a side the store
    leaves without a reference or whose configured reference it drops.
    """

    VALUES = ("", "1", "2", "3")

    def store_channels(self, store, *, mic, shared, left, right, side):
        async def setup():
            try:
                result = await store._prepare_measurement_job_setup(
                    input_id="mic", input_key="", mic_input_channel=mic,
                    reference_input_channel=shared, reference_input_channel_left=left,
                    reference_input_channel_right=right,
                    reference_candidate_channels=reference_candidate_channels(shared, left, right),
                    channel=side, calibration_filename=None, calibration_bytes=None,
                    calibration_ref=None, measurement_scope="active-chain",
                    job_prefix="measurement-job-")
                return result["job"]["input_channels"]
            finally:
                # Preparation alone claims the single-job start slot; a real
                # start hands it to _register_measurement_job. This parity
                # probe never registers, so release it for the next take.
                store._release_measurement_start_slot()
        return asyncio.run(setup())

    def test_start_check_matches_the_store_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MeasurementStore(home=Path(directory))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 4, "sample_rate": 48_000, "available": True}]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs
            checked = 0
            for mic, shared, left, right, side in itertools.product(
                    ("", "1", "3"), self.VALUES, self.VALUES, self.VALUES, ("left", "right")):
                channels = self.store_channels(store, mic=mic, shared=shared, left=left,
                                               right=right, side=side)
                side_reference = channels[f"electrical_reference_{side}"]
                dropped = bool(channels[f"reference_disabled_reason_{side}"])
                with self.subTest(mic=mic, shared=shared, left=left, right=right, side=side):
                    try:
                        accepted = speaker(side, mic=mic, shared=shared, left=left, right=right)
                    except ValueError:
                        self.assertTrue(side_reference is None or dropped, channels)
                    else:
                        self.assertEqual(accepted, side_reference, channels)
                        self.assertIn(accepted, channels["electrical_reference_candidates"])
                        self.assertFalse(dropped, channels)
                checked += 1
            self.assertEqual(checked, 3 * 4 ** 3 * 2)


if __name__ == "__main__":
    unittest.main()
