#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Electrical reference channels resolve the same way for store and Speaker Align.

The store resolves the reference of every take with ``resolve_reference_channels``;
the Speaker Align start check judges the same resolution before any sweep, so a
reference that the take would drop never starts a run.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.reference_channels import (
    require_speaker_align_reference,
    resolve_reference_channels,
)


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
                with self.assertRaisesRegex(ValueError, "is the microphone input"):
                    speaker("left", **options)

    def test_split_reference_resolves_per_side(self):
        self.assertEqual(speaker("left", shared="7", left="7", right="8"), 7)
        self.assertEqual(speaker("right", shared="7", left="7", right="8"), 8)

    def test_a_configured_side_on_the_microphone_channel_is_refused(self):
        # The store would silently record the other side's loopback instead.
        with self.assertRaisesRegex(ValueError, "is the microphone input"):
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
                with self.assertRaisesRegex(ValueError, "is the microphone input"):
                    speaker(side, left="1")

    def test_a_stale_shared_value_on_the_microphone_does_not_block_split_references(self):
        # With per-side references set the shared field serves no side.
        self.assertEqual(speaker("left", shared="1", left="7", right="8"), 7)
        self.assertEqual(speaker("right", shared="1", left="7", right="8"), 8)

    def test_no_reference_is_refused(self):
        for options in ({}, {"shared": None}, {"shared": " ", "left": "", "right": None}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "requires an electrical reference"):
                    speaker("left", **options)

    def test_malformed_channel_is_refused(self):
        for options in ({"shared": "x"}, {"shared": "0"}, {"mic": "-1", "shared": "2"}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "input channel number"):
                    speaker("left", **options)


if __name__ == "__main__":
    unittest.main()
