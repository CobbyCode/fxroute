# SPDX-License-Identifier: AGPL-3.0-only

"""Independent contract tests for the remote-volume soft-pickup invariant.

When provider and FXRoute master levels disagree, soft pickup applies:
the connect/reconnect push only anchors, far-side gestures never move the
master, and only a gesture crossing the master level (bounded overshoot)
takes over. Afterwards real remote changes drive the global master.
Pre-gain and loudness volumeDb are never touched.

Covers Spotify and Qobuz separately.
"""

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
from playback.remote_volume import RemoteVolumePickupTranslator
from playback.spotifyd_volume_watch import SpotifydVolumeWatchDependencies


def _pickup_translator(applied, active=True, master=37):
    async def apply_value(value):
        applied.append(value)

    return RemoteVolumePickupTranslator(
        is_active=lambda: active,
        apply_volume_value=apply_value,
        current_master=lambda: master,
    )


class TranslatorSurfaceTests(unittest.TestCase):
    def test_pickup_semantics_are_canonical(self):
        self.assertTrue(hasattr(remote_volume, "RemoteVolumePickupTranslator"))
        self.assertTrue(hasattr(remote_volume, "MAX_PICKUP_OVERSHOOT"))
        self.assertFalse(hasattr(remote_volume, "RemoteVolumeDeltaTranslator"))
        self.assertFalse(hasattr(remote_volume, "RemoteVolumeAbsoluteTranslator"))

    def test_qobuz_uses_pickup_translator(self):
        self.assertIs(QobuzRemoteVolumeTranslator, RemoteVolumePickupTranslator)

    def test_qobuz_deps_pin_pickup_surface(self):
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_value", "current_master", "on_device_active"},
        )

    def test_spotify_deps_pin_pickup_surface(self):
        fields = set(SpotifydVolumeWatchDependencies.__dataclass_fields__.keys())
        self.assertIn("apply_volume_value", fields)
        self.assertIn("current_master", fields)
        self.assertNotIn("apply_volume_delta", fields)

    def test_main_uses_absolute_writer_only(self):
        self.assertTrue(hasattr(main, "_apply_remote_volume_value"))
        self.assertFalse(hasattr(main, "_apply_remote_volume_delta"))


class QobuzPickupContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_push_never_jumps_master(self):
        applied = []
        translator = _pickup_translator(applied, master=37)
        self.assertFalse(translator.submit(98))
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertFalse(translator.picked_up)

    async def test_far_side_gesture_stays_silent(self):
        applied = []
        translator = _pickup_translator(applied, master=37)
        translator.submit(98)
        for value in (90, 70, 50, 38):
            translator.submit(value)
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertFalse(translator.picked_up)

    async def test_crossing_takes_over_and_tracks(self):
        applied = []
        translator = _pickup_translator(applied, master=37)
        translator.submit(98)
        translator.submit(37)
        await translator.flush()
        translator.submit(30)
        await translator.flush()
        self.assertEqual(applied, [37, 30])
        self.assertTrue(translator.picked_up)

    async def test_reconnect_rearms_without_write(self):
        applied = []
        translator = _pickup_translator(applied, master=13)
        translator.submit(1)
        translator.submit(13)
        await translator.flush()
        translator.observe_activation()
        translator.submit(13)
        await translator.flush()
        self.assertEqual(applied, [13])
        self.assertFalse(translator.picked_up)

    async def test_owner_loss_discards_pending(self):
        applied = []
        translator = _pickup_translator(applied, active=True, master=13)
        translator.submit(1)
        translator.submit(13)
        translator.is_active = lambda: False
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertIsNone(translator.pending)


class SpotifyPickupContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_value_anchors(self):
        applied = []
        translator = _pickup_translator(applied, master=44)
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_mid_session_push_is_ignored(self):
        applied = []
        translator = _pickup_translator(applied, master=44)
        translator.submit(42)
        translator.submit(100)
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertFalse(translator.picked_up)

    async def test_overshoot_bound_rejects_leap(self):
        applied = []
        translator = _pickup_translator(applied, master=44)
        translator.submit(42)
        translator.submit(56)
        self.assertFalse(translator.picked_up)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_step_within_bound_picks_up(self):
        applied = []
        translator = _pickup_translator(applied, master=44)
        translator.submit(42)
        translator.submit(52)
        self.assertTrue(translator.picked_up)
        await translator.flush()
        self.assertEqual(applied, [52])

    async def test_reconnect_rearms_pickup(self):
        applied = []
        translator = _pickup_translator(applied, master=44)
        translator.submit(42)
        translator.submit(44)
        await translator.flush()
        translator.observe_activation()
        translator.submit(44)
        await translator.flush()
        self.assertEqual(applied, [44])
        self.assertFalse(translator.picked_up)


class MasterWriterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_absolute_value_lands_on_master(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value="qobuz"), mock.patch.object(
            main, "get_output_volume_safe", return_value=20
        ), mock.patch.object(main, "set_output_volume", return_value=98) as set_volume:
            await main._apply_remote_volume_value(98, owner="qobuz")
        set_volume.assert_called_once_with(98)

    async def test_non_owner_never_writes(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value="spotify"), mock.patch.object(
            main, "set_output_volume"
        ) as set_volume:
            await main._apply_remote_volume_value(45, owner="qobuz")
        set_volume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
