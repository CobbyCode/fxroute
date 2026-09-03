# SPDX-License-Identifier: AGPL-3.0-only

"""Independent contract tests for the remote-volume delta invariant.

Provider volume deltas drive the global FXRoute master only. The first
provider value anchors without a master write; later values apply as a
relative delta against the running anchor. Provider/pre gain, unity pins
and loudness volumeDb are never touched.

Covers Spotify and Qobuz separately: connect/reconnect, app push,
owner change, roundtrip and feedback-loop protection.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from playback import remote_volume
from playback.qbzd_volume_watch import (
    QobuzRemoteVolumeTranslator,
    QobuzVolumeWatchDependencies,
)
from playback.remote_volume import RemoteVolumeDeltaTranslator
from playback.spotifyd_volume_watch import SpotifydVolumeWatchDependencies


def _delta_translator(applied, active=True):
    async def apply_delta(delta):
        applied.append(delta)

    return RemoteVolumeDeltaTranslator(
        is_active=lambda: active,
        apply_volume_delta=apply_delta,
    )


class TranslatorSurfaceTests(unittest.TestCase):
    def test_no_absolute_or_pickup_semantics_remain(self):
        self.assertFalse(hasattr(remote_volume, "RemoteVolumeAbsoluteTranslator"))
        self.assertFalse(hasattr(remote_volume, "RemoteVolumePickupTranslator"))
        self.assertFalse(hasattr(remote_volume, "MAX_PICKUP_OVERSHOOT"))

    def test_qobuz_uses_delta_translator(self):
        self.assertIs(QobuzRemoteVolumeTranslator, RemoteVolumeDeltaTranslator)

    def test_qobuz_deps_pin_delta_surface(self):
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_delta", "on_device_active"},
        )

    def test_spotify_deps_pin_delta_surface(self):
        fields = set(SpotifydVolumeWatchDependencies.__dataclass_fields__.keys())
        self.assertIn("apply_volume_delta", fields)
        self.assertNotIn("apply_volume_value", fields)
        self.assertNotIn("current_master", fields)

    def test_main_uses_delta_writer_only(self):
        self.assertTrue(hasattr(main, "_apply_remote_volume_delta"))
        self.assertFalse(hasattr(main, "_apply_remote_volume_value"))


class QobuzDeltaContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_push_anchors_without_master_write(self):
        applied = []
        translator = _delta_translator(applied)
        self.assertFalse(translator.submit(100))
        self.assertTrue(translator.anchored)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_app_push_never_teleports_master(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        self.assertTrue(translator.submit(96))
        await translator.flush()
        self.assertEqual(applied, [-4])

    async def test_burst_applies_net_delta_once(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.submit(99)
        translator.submit(98)
        self.assertEqual(translator.pending, -2)
        await translator.flush()
        self.assertEqual(applied, [-2])

    async def test_roundtrip_nets_zero(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.submit(96)
        translator.submit(100)
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_reconnect_reanchors(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.submit(96)
        translator.observe_activation()
        self.assertFalse(translator.anchored)
        self.assertFalse(translator.submit(37))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_owner_change_discards_pending(self):
        applied = []
        translator = _delta_translator(applied, active=True)
        translator.submit(100)
        translator.submit(96)
        translator.is_active = lambda: False
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_feedback_repeat_produces_no_write(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(applied, [])


class SpotifyDeltaContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_sync_anchors(self):
        applied = []
        translator = _delta_translator(applied)
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_gesture_applies_delta_not_absolute(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.submit(96)
        await translator.flush()
        self.assertEqual(applied, [-4])

    async def test_stepwise_observations_apply_per_step(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.submit(99)
        await translator.flush()
        translator.submit(98)
        await translator.flush()
        self.assertEqual(applied, [-1, -1])

    async def test_reconnect_reanchors_without_write(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(100)
        translator.observe_activation()
        self.assertFalse(translator.submit(45))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_inactive_owner_resets_anchor(self):
        applied = []
        translator = _delta_translator(applied, active=False)
        self.assertFalse(translator.submit(70))
        self.assertFalse(translator.anchored)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_no_feedback_loop_from_repeats(self):
        applied = []
        translator = _delta_translator(applied)
        translator.submit(50)
        translator.submit(45)
        await translator.flush()
        self.assertFalse(translator.submit(45))
        await translator.flush()
        self.assertEqual(applied, [-5])


class MasterWriterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_delta_lands_on_current_master(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value="qobuz"), mock.patch.object(
            main, "get_output_volume_safe", return_value=40
        ), mock.patch.object(main, "set_output_volume", return_value=45) as set_volume:
            await main._apply_remote_volume_delta(5, owner="qobuz")
        set_volume.assert_called_once_with(45)

    async def test_delta_clamps_to_master_bounds(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value="spotify"), mock.patch.object(
            main, "get_output_volume_safe", return_value=98
        ), mock.patch.object(main, "set_output_volume", return_value=100) as set_volume:
            await main._apply_remote_volume_delta(10, owner="spotify")
        set_volume.assert_called_once_with(100)

    async def test_non_owner_never_writes(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value="spotify"), mock.patch.object(
            main, "set_output_volume"
        ) as set_volume:
            await main._apply_remote_volume_delta(5, owner="qobuz")
        set_volume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
