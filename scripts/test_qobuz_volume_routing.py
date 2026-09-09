# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for Qobuz volume routing on the FXRoute side.

Verifies the chosen locked+journal design surface without a qbzd daemon:
* ``get_qobuz_ui_state`` reports the canonical FXRoute master as ``volume``
  (the raw qbzd value stays visible as ``source_volume``), so 100% on the
  Qobuz client maps semantically 1:1 onto 100% FXRoute master.
* ``/api/streaming/qobuz/volume`` writes the canonical master and never the
  qbzd engine volume (which must remain pinned at 100 / unity).
* Claim/start paths re-pin qbzd's gain to 100 %.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import streaming as streaming_module
import streaming.api as streaming_api


def _qobuz_state(**overrides):
    state = {
        "source": "qobuz", "available": True, "status": "Playing",
        "title": "T", "artist": "A", "album": "L", "trackId": "42",
        "artUrl": "http://art/c.jpg", "volume": 42, "queue_len": 7,
    }
    state.update(overrides)
    return state


class QobuzUIVolumeStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_state_reports_master_as_volume_and_keeps_raw_source(self):
        main.playback_state.current_playback_owner = None
        provider = mock.Mock()
        provider.status = mock.AsyncMock(return_value=_qobuz_state(volume=42))
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=38):
            state = await main.get_qobuz_ui_state()

        self.assertEqual(state["volume"], 38)
        self.assertEqual(state["source_volume"], 42)
        self.assertEqual(state["title"], "T")

    async def test_ui_state_volume_falls_back_when_no_raw_volume(self):
        main.playback_state.current_playback_owner = None
        provider = mock.Mock()
        provider.status = mock.AsyncMock(return_value=_qobuz_state(volume=None))
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=55):
            state = await main.get_qobuz_ui_state()
        self.assertEqual(state["volume"], 55)
        self.assertIsNone(state["source_volume"])


class QobuzVolumeRouteTests(unittest.IsolatedAsyncioTestCase):
    def _deps(self, **overrides):
        base = dict(
            get_qobuz_ui_state=mock.AsyncMock(return_value={}),
            is_qobuz_playback_active=lambda state: False,
            qobuz_pause=mock.AsyncMock(),
            broadcast_qobuz_state=mock.AsyncMock(),
            qobuz_target_track_from_state=lambda state: {},
            qobuz_target_rate=lambda state: 44100,
            qobuz_pin_unity=mock.AsyncMock(),
            qobuz_volume_action=mock.AsyncMock(),
            spotify_volume_action=mock.AsyncMock(),
            publish_committed_playback_owner=mock.AsyncMock(),
            transition_error_http=lambda exc: exc,
            request_origin_is_trusted=lambda req: True,
            coordinator_rate_change=lambda rate: None,
            run_coordinated_transition=mock.AsyncMock(),
            get_current_playback_owner=lambda: None,
            spotify_playerctl_watch=mock.Mock(),
            api_spotify_play=mock.AsyncMock(),
            api_spotify_toggle=mock.AsyncMock(),
        )
        base.update(overrides)
        return streaming_api.StreamingApiDeps(**base)

    async def test_qobuz_volume_routes_to_canonical_master(self):
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 60})
        provider.status = mock.AsyncMock(return_value=_qobuz_state(volume=42))

        class _Request:
            async def json(self):
                return {"volume": 60}

        canon = mock.AsyncMock(return_value={"volume": 60, "loudness_enabled": False})
        deps = self._deps(qobuz_volume_action=canon)
        orig = streaming_api._runtime.deps
        streaming_api.configure_streaming_api(deps)
        try:
            with mock.patch.object(streaming_module, "get_provider", return_value=provider):
                result = await streaming_api.api_streaming_provider_action("qobuz", "volume", _Request())
        finally:
            streaming_api.configure_streaming_api(orig)

        canon.assert_awaited_once_with(60)
        provider.set_volume.assert_not_awaited()
        self.assertEqual(result["volume"], 60)

    async def test_spotify_volume_routes_to_canonical_master_too(self):
        # spotifyd runs with volume_controller=none: its Connect volume is a
        # reported value only, so the slider drives the FXRoute master exactly
        # like the Qobuz slider. No source write may happen.
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 70})

        class _Request:
            async def json(self):
                return {"volume": 70}

        canon = mock.AsyncMock(return_value={"volume": 70, "source_volume": 55})
        deps = self._deps(spotify_volume_action=canon)
        orig = streaming_api._runtime.deps
        streaming_api.configure_streaming_api(deps)
        try:
            with mock.patch.object(streaming_module, "get_provider", return_value=provider):
                result = await streaming_api.api_streaming_provider_action("spotify", "volume", _Request())
        finally:
            streaming_api.configure_streaming_api(orig)

        canon.assert_awaited_once_with(70)
        provider.set_volume.assert_not_awaited()
        self.assertEqual(result["volume"], 70)
        self.assertEqual(result["source_volume"], 55)


class QobuzUnityPinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_pin_state = main.qobuz_unity_pin_state
        main.qobuz_unity_pin_state = None
        self._orig_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        main.qobuz_unity_pin_state = self._orig_pin_state
        main.playback_state.current_playback_owner = self._orig_owner

    async def test_pin_unity_writes_qbzd_engine_volume_100(self):
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 100})
        with mock.patch.object(main.streaming, "get_provider", return_value=provider):
            await main._qobuz_pin_unity()
        provider.set_volume.assert_awaited_once_with(100)

    async def test_pin_success_records_health_ok(self):
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 100})
        with mock.patch.object(main.streaming, "get_provider", return_value=provider):
            await main._qobuz_pin_unity()
        self.assertIsNotNone(main.qobuz_unity_pin_state)
        self.assertTrue(main.qobuz_unity_pin_state["ok"])

    async def test_pin_failure_records_health_error_without_raising(self):
        # Regression: a failed unity pin is not a silent no-op. The health
        # state turns to "error" (visible in the UI state), the failure is
        # logged, and playback ownership stays untouched.
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(side_effect=RuntimeError("qbzd unreachable"))
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main.logger, "error") as log_error:
            await main._qobuz_pin_unity()
        self.assertFalse(main.qobuz_unity_pin_state["ok"])
        self.assertIn("error", main.qobuz_unity_pin_state)
        log_error.assert_called_once()
        self.assertIsNone(main.playback_state.current_playback_owner)

    async def test_pin_retry_recovers_after_failure(self):
        # The next pin call (claim/UI-start) is the controlled retry path.
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(side_effect=[RuntimeError("down"), {"volume": 100}])
        with mock.patch.object(main.streaming, "get_provider", return_value=provider):
            await main._qobuz_pin_unity()
            self.assertFalse(main.qobuz_unity_pin_state["ok"])
            await main._qobuz_pin_unity()
        self.assertTrue(main.qobuz_unity_pin_state["ok"])

    async def test_ui_state_exposes_unity_pin_health_flag(self):
        provider = mock.Mock()
        provider.status = mock.AsyncMock(return_value=_qobuz_state(volume=42))
        main.qobuz_unity_pin_state = {"ok": True, "at": 1.0}
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=38):
            state = await main.get_qobuz_ui_state()
        self.assertEqual(state["qobuz_unity_pin"], "ok")

        main.qobuz_unity_pin_state = {"ok": False, "error": "boom", "at": 1.0}
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=38):
            state = await main.get_qobuz_ui_state()
        self.assertEqual(state["qobuz_unity_pin"], "error")

        main.qobuz_unity_pin_state = None
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=38):
            state = await main.get_qobuz_ui_state()
        self.assertIsNone(state["qobuz_unity_pin"])

    async def test_claim_pins_unity_after_commit(self):
        playing = _qobuz_state(status="Playing", trackId="42")
        committed = type("Result", (), {"committed": True, "transition_id": "t1"})()
        with mock.patch.object(main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=playing)), \
             mock.patch.object(main, "_run_coordinated_transition",
                               new=mock.AsyncMock(return_value=committed)), \
             mock.patch.object(main, "broadcast_qobuz_state",
                               new=mock.AsyncMock(return_value=playing)), \
             mock.patch.object(main, "_qobuz_pin_unity", new=mock.AsyncMock()) as pin:
            main.playback_state.current_playback_owner = None
            await main._claim_qobuz_playback("qbzd-playing")
        pin.assert_awaited_once()

    async def test_ui_start_pins_unity_after_commit(self):
        playing = _qobuz_state(status="Paused", trackId="42")
        committed = type("Result", (), {"committed": True, "transition_id": "t2"})()
        pin = mock.AsyncMock()
        deps = streaming_api.StreamingApiDeps(
            get_qobuz_ui_state=mock.AsyncMock(return_value=playing),
            is_qobuz_playback_active=lambda state: False,
            qobuz_pause=mock.AsyncMock(),
            broadcast_qobuz_state=mock.AsyncMock(return_value=playing),
            qobuz_target_track_from_state=lambda state: {"id": "42"},
            qobuz_target_rate=lambda state: 44100,
            qobuz_pin_unity=pin,
            qobuz_volume_action=mock.AsyncMock(),
            spotify_volume_action=mock.AsyncMock(),
            publish_committed_playback_owner=mock.AsyncMock(),
            transition_error_http=lambda exc: exc,
            request_origin_is_trusted=lambda req: True,
            coordinator_rate_change=lambda rate: None,
            run_coordinated_transition=mock.AsyncMock(return_value=committed),
            get_current_playback_owner=lambda: None,
            spotify_playerctl_watch=mock.Mock(),
            api_spotify_play=mock.AsyncMock(),
            api_spotify_toggle=mock.AsyncMock(),
        )
        orig = streaming_api._runtime.deps
        streaming_api.configure_streaming_api(deps)
        try:
            from streaming.qobuz import connect_state as qobuz_connect_state

            qobuz_connect_state.reset()
            await streaming_api._qobuz_ui_start_action("play")
            qobuz_connect_state.reset()
        finally:
            streaming_api.configure_streaming_api(orig)
        pin.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()