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
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main


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
    async def test_qobuz_volume_routes_to_canonical_master(self):
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 60})
        provider.status = mock.AsyncMock(return_value=_qobuz_state(volume=42))

        class _Request:
            async def json(self):
                return {"volume": 60}

        volume_result = {"volume": 60, "loudness_enabled": False}
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "_set_canonical_output_volume",
                               new=mock.AsyncMock(return_value=volume_result)) as canon, \
             mock.patch.object(main, "_resolve_playback_owner", return_value="qobuz"):
            result = await main.api_streaming_provider_action("qobuz", "volume", _Request())

        canon.assert_awaited_once_with(60)
        provider.set_volume.assert_not_awaited()
        self.assertEqual(result["volume"], 60)

    async def test_other_provider_volume_keeps_provider_dispatch(self):
        provider = mock.Mock()
        provider.set_volume = mock.AsyncMock(return_value={"volume": 70})

        class _Request:
            async def json(self):
                return {"volume": 70}

        with mock.patch.object(main.streaming, "get_provider", return_value=provider):
            result = await main.api_streaming_provider_action("spotify", "volume", _Request())

        provider.set_volume.assert_awaited_once_with(70)
        self.assertEqual(result["volume"], 70)

    async def test_pickup_generation_is_rechecked_after_dsp_lock(self):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
            generation = translator.pending_generation
            dsp_lock = main._dsp_mutation_lock()
            await dsp_lock.acquire()
            try:
                writes = []

                async def fake_drain(func, *args, **kwargs):
                    writes.append((func, args))
                    return None

                with mock.patch.object(
                    main, "_volume_state_for_manager",
                    new=mock.AsyncMock(return_value=main.volume_contract.VolumeState(
                        preset="Direct", loudness_enabled=False, volume_db=0.0, master_percent=30
                    )),
                ), mock.patch.object(main, "_drain_worker", new=fake_drain):
                    task = asyncio.create_task(
                        main._set_canonical_output_volume(29, pickup_generation=generation)
                    )
                    await asyncio.sleep(0)
                    translator.reset()  # owner/provider invalidation while waiting
                    dsp_lock.release()
                    result = await task
            finally:
                if dsp_lock.locked():
                    dsp_lock.release()

            self.assertTrue(result["stale_pickup"])
            self.assertEqual(writes, [])
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def _run_stale_pickup_after_state_read(self, *, loudness: bool):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        state_entered = asyncio.Event()
        release_state = asyncio.Event()
        writes = []

        async def blocked_state(*args, **kwargs):
            state_entered.set()
            await release_state.wait()
            return main.volume_contract.VolumeState(
                preset="Default" if loudness else "Direct",
                loudness_enabled=loudness,
                volume_db=0.0,
                master_percent=30,
            )

        async def fake_drain(func, *args, **kwargs):
            writes.append((func, args))
            return {"runtime_applied": True, "extras": {"loudness": {"params": {"volumeDb": 0.0}}}}

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                 mock.patch.object(main, "_volume_state_for_manager", new=blocked_state), \
                 mock.patch.object(main, "_drain_worker", new=fake_drain), \
                 mock.patch.object(main, "dsp_manager", mock.Mock() if loudness else None):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
                generation = translator.pending_generation
                task = asyncio.create_task(
                    main._set_canonical_output_volume(29, pickup_generation=generation)
                )
                await state_entered.wait()
                translator.reset()  # owner switch invalidates Qobuz pickup
                release_state.set()
                result = await task

            self.assertTrue(result["stale_pickup"])
            self.assertEqual(writes, [])
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def test_pickup_invalidated_during_state_read_skips_direct_write(self):
        await self._run_stale_pickup_after_state_read(loudness=False)

    async def test_pickup_invalidated_during_state_read_skips_loudness_write(self):
        await self._run_stale_pickup_after_state_read(loudness=True)

    async def _run_remote_write_barrier(self, *, loudness: bool, fail: bool = False):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        started = asyncio.Event()
        release = asyncio.Event()
        writes = []

        manager = mock.Mock() if loudness else None
        state = main.volume_contract.VolumeState(
            preset="Default" if loudness else "Direct",
            loudness_enabled=loudness,
            volume_db=0.0,
            master_percent=30,
        )

        async def drain(func, *args, **kwargs):
            name = getattr(func, "_mock_name", None) or getattr(func, "__name__", "")
            is_loudness_write = loudness and name == "set_loudness_volume_db"
            is_master_write = name == "set_output_volume"
            if is_loudness_write or (is_master_write and args == (29,)):
                started.set()
                await release.wait()
                if fail:
                    raise RuntimeError("volume write failed")
            if is_master_write:
                writes.append(args[0])
            if is_loudness_write:
                return {"runtime_applied": True, "extras": {"loudness": {"params": {"volumeDb": 0.0}}}}
            return args[0] if args else None

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                 mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                 mock.patch.object(main, "_drain_worker", new=drain), \
                 mock.patch.object(main, "dsp_manager", manager):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
                generation = translator.pending_generation
                watch = main.qobuz_volume_watch
                watch._schedule_drain()
                first_drain = watch._drain_task
                await started.wait()
                self.assertTrue(translator.picked_up)
                self.assertEqual(translator.pending, None)
                self.assertEqual(translator._generation, generation)

                self.assertTrue(
                    translator.submit(28),
                    f"picked_up={translator.picked_up} last={translator._last_canonical} "
                    f"target={translator._pickup_target} generation={translator._generation}",
                )
                watch._schedule_drain()
                self.assertEqual(translator.pending, 28)
                self.assertEqual(translator._generation, generation)
                release.set()
                await first_drain

                self.assertEqual(writes, [28] if fail else ([29, 28] if not loudness else [100, 100]))
                self.assertIsNone(translator.pending)
                self.assertTrue(translator.picked_up)
                self.assertEqual(translator._generation, generation)
                self.assertEqual(translator._last_canonical, 28)
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def test_real_direct_remote_write_publishes_after_write_then_drains_next_step(self):
        await self._run_remote_write_barrier(loudness=False)

    async def test_real_loudness_remote_write_publishes_after_mutation_then_drains_next_step(self):
        await self._run_remote_write_barrier(loudness=True)

    async def test_loudness_publishes_before_blocked_master_pin_and_drains_next_step(self):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        canonical = 30
        pin_started = asyncio.Event()
        release_pin = asyncio.Event()
        pin_count = 0
        loudness_count = 0
        writes = []
        manager = mock.Mock()
        state = main.volume_contract.VolumeState(
            preset="Default", loudness_enabled=True, volume_db=0.0, master_percent=30
        )

        async def drain(func, *args, **kwargs):
            nonlocal canonical, pin_count, loudness_count
            name = getattr(func, "_mock_name", None) or getattr(func, "__name__", "")
            if name == "set_loudness_volume_db":
                loudness_count += 1
                canonical = 29 if loudness_count == 1 else 28
                return {"runtime_applied": True, "extras": {"loudness": {"params": {"volumeDb": args[0]}}}}
            if name == "set_output_volume":
                pin_count += 1
                writes.append(args[0])
                if pin_count == 1:
                    pin_started.set()
                    await release_pin.wait()
            return args[0] if args else None

        try:
            with mock.patch.object(main, "get_output_volume_safe", side_effect=lambda default=100: canonical), \
                 mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                 mock.patch.object(main, "_drain_worker", new=drain), \
                 mock.patch.object(main, "dsp_manager", manager):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
                generation = translator.pending_generation
                watch = main.qobuz_volume_watch
                watch._schedule_drain()
                first_drain = watch._drain_task
                await pin_started.wait()
                self.assertEqual(canonical, 29)
                self.assertEqual(translator._last_canonical, 29)
                self.assertTrue(translator.picked_up)
                self.assertTrue(translator.submit(28))
                watch._schedule_drain()
                self.assertEqual(translator.pending, 28)
                self.assertEqual(translator._generation, generation)
                release_pin.set()
                await first_drain

                self.assertEqual(canonical, 28)
                self.assertEqual(translator._last_canonical, 28)
                self.assertEqual(writes, [100, 100])
                self.assertIsNone(translator.pending)
                self.assertTrue(translator.picked_up)
                self.assertEqual(translator._generation, generation)
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def test_loudness_write_failure_does_not_publish_canonical_value(self):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        state = main.volume_contract.VolumeState(
            preset="Default", loudness_enabled=True, volume_db=0.0, master_percent=30
        )

        async def failing_drain(*args, **kwargs):
            raise RuntimeError("loudness write failed")

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                 mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                 mock.patch.object(main, "_drain_worker", new=failing_drain), \
                 mock.patch.object(main, "dsp_manager", mock.Mock()):
                translator.submit(90)
                translator.submit(29)
                generation = translator.pending_generation
                with self.assertRaises(RuntimeError):
                    await main._set_canonical_output_volume(29, pickup_generation=generation)

            self.assertEqual(translator._last_canonical, 30)
            self.assertEqual(translator._generation, generation)
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def test_pin_failure_keeps_successfully_published_loudness_value(self):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        state = main.volume_contract.VolumeState(
            preset="Default", loudness_enabled=True, volume_db=0.0, master_percent=30
        )
        manager = mock.Mock()
        loudness_written = False

        async def drain(func, *args, **kwargs):
            nonlocal loudness_written
            name = getattr(func, "_mock_name", None) or getattr(func, "__name__", "")
            if name == "set_loudness_volume_db":
                loudness_written = True
                return {"runtime_applied": True, "extras": {"loudness": {"params": {"volumeDb": args[0]}}}}
            raise RuntimeError("master pin failed")

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                 mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                 mock.patch.object(main, "_drain_worker", new=drain), \
                 mock.patch.object(main, "dsp_manager", manager):
                translator.submit(90)
                translator.submit(29)
                generation = translator.pending_generation
                with self.assertRaises(RuntimeError):
                    await main._set_canonical_output_volume(29, pickup_generation=generation)

            self.assertTrue(loudness_written)
            self.assertEqual(translator._last_canonical, 29)
            self.assertEqual(translator._generation, generation)
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()

    async def _assert_owner_publish_waits_for_remote_write(
        self, *, loudness: bool, local_commit: bool = False, stop: bool = False
    ):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        main.runtime.canonical_volume_write_lock = None
        main.playback_state.current_playback_owner = "qobuz"
        translator.reset()
        started = asyncio.Event()
        release = asyncio.Event()
        state = main.volume_contract.VolumeState(
            preset="Default" if loudness else "Direct",
            loudness_enabled=loudness,
            volume_db=0.0,
            master_percent=30,
        )
        manager = mock.Mock()
        manager.broadcast = mock.AsyncMock()
        writes = []

        async def drain(func, *args, **kwargs):
            name = getattr(func, "_mock_name", None) or getattr(func, "__name__", "")
            blocking_mutation = (
                loudness and name == "set_loudness_volume_db"
            ) or (not loudness and name == "set_output_volume" and args == (29,))
            if blocking_mutation:
                started.set()
                await release.wait()
            if name == "set_output_volume":
                writes.append(args[0])
            if loudness and name == "set_loudness_volume_db":
                return {"runtime_applied": True, "extras": {"loudness": {"params": {"volumeDb": 0.0}}}}
            return args[0] if args else None

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                 mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                 mock.patch.object(main, "_drain_worker", new=drain), \
                 mock.patch.object(main, "dsp_manager", manager if loudness else None), \
                 mock.patch.object(main, "manager", manager), \
                 mock.patch.object(main, "build_playback_payload", return_value={}):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
                watch = main.qobuz_volume_watch
                watch._schedule_drain()
                remote_task = watch._drain_task
                await started.wait()

                if local_commit:
                    with mock.patch.object(main, "_record_local_track_started"):
                        owner_task = asyncio.create_task(
                            main._commit_coordinated_track(
                                {"source": "local", "id": "local-test", "url": "/music/local-test.flac"},
                                source="local", commit_token="owner-test",
                            )
                        )
                elif stop:
                    fake_player = SimpleNamespace(
                        _running=True, state={}, stop_playback=mock.Mock()
                    )
                    with mock.patch.object(main.runtime, "player_instance", fake_player), \
                         mock.patch.object(main, "_playback_transition_is_active", return_value=False), \
                         mock.patch.object(main, "get_samplerate_status", return_value={}), \
                         mock.patch.object(main.samplerate, "clear_auto_policy_force_rate"):
                        owner_task = asyncio.create_task(main.stop_playback())
                        await asyncio.sleep(0)
                        self.assertEqual(main.playback_state.current_playback_owner, "qobuz")
                        self.assertFalse(owner_task.done())
                        release.set()
                        await remote_task
                        await owner_task
                        self.assertEqual(main.playback_state.current_playback_owner, None)
                        return
                else:
                    owner_task = asyncio.create_task(
                        main._publish_committed_playback_owner("spotify", "owner-test")
                    )
                await asyncio.sleep(0)
                self.assertEqual(main.playback_state.current_playback_owner, "qobuz")
                self.assertFalse(owner_task.done())

                release.set()
                await remote_task
                await owner_task
                self.assertEqual(
                    main.playback_state.current_playback_owner,
                    "local" if local_commit else "spotify",
                )
        finally:
            main.playback_state.current_playback_owner = original_owner
            translator.reset()
            main.runtime.canonical_volume_write_lock = None

    async def test_owner_publish_waits_for_direct_remote_write(self):
        await self._assert_owner_publish_waits_for_remote_write(loudness=False)

    async def test_owner_publish_waits_for_loudness_remote_write(self):
        await self._assert_owner_publish_waits_for_remote_write(loudness=True)

    async def test_local_track_commit_waits_for_direct_remote_write(self):
        await self._assert_owner_publish_waits_for_remote_write(
            loudness=False, local_commit=True
        )

    async def test_stop_waits_for_direct_remote_write(self):
        await self._assert_owner_publish_waits_for_remote_write(
            loudness=False, stop=True
        )

    async def test_failed_real_remote_write_does_not_publish_canonical_value(self):
        await self._run_remote_write_barrier(loudness=False, fail=True)


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
        with mock.patch.object(main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=playing)), \
             mock.patch.object(main, "_run_coordinated_transition",
                               new=mock.AsyncMock(return_value=committed)), \
             mock.patch.object(main, "broadcast_qobuz_state",
                               new=mock.AsyncMock(return_value=playing)), \
             mock.patch.object(main, "_qobuz_pin_unity", new=mock.AsyncMock()) as pin:
            main.playback_state.current_playback_owner = None
            await main._qobuz_ui_start_action("play")
        pin.assert_awaited_once()


class AtomicPlaybackCommitTests(unittest.IsolatedAsyncioTestCase):
    """Qobuz -> Local/Spotify/Stop/Restore/Abort must be one atomic app commit.

    The running canonical volume commit (Qobuz pickup) holds the canonical
    volume lock.  Until it finishes, a source switch must not publish a mixed
    state (new Local track + old Qobuz owner + old commit token).
    """

    def _fake_player(self):
        return SimpleNamespace(_running=True, state={}, stop_playback=mock.Mock())

    async def _hold_canonical_lock_and_switch(
        self, *, owner_action, expect_owner, expect_track_source=None,
        extra_assert=None
    ):
        translator = main.qobuz_volume_watch._translator
        original_owner = main.playback_state.current_playback_owner
        original_track = dict(main.playback_state.current_track_info or {})
        original_commit = main.playback_state.playback_context_commit_id
        main.runtime.canonical_volume_write_lock = None
        main.playback_state.current_playback_owner = "qobuz"
        main.playback_state.current_track_info = {"source": "qobuz", "id": "qobuz-old", "url": "qobuz://old"}
        main.playback_state.playback_context_commit_id = "commit-old"
        translator.reset()

        started = asyncio.Event()
        release = asyncio.Event()
        state = main.volume_contract.VolumeState(
            preset="Direct", loudness_enabled=False, volume_db=0.0, master_percent=30
        )

        async def drain(func, *args, **kwargs):
            name = getattr(func, "_mock_name", None) or getattr(func, "__name__", "")
            if name == "set_output_volume" and args == (29,):
                started.set()
                await release.wait()
            if name == "set_output_volume":
                return args[0] if args else None
            return args[0] if args else None

        try:
            with mock.patch.object(main, "get_output_volume_safe", return_value=30), \
                  mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock(return_value=state)), \
                  mock.patch.object(main, "_drain_worker", new=drain), \
                  mock.patch.object(main, "dsp_manager", None):
                translator.submit(90)
                self.assertTrue(translator.submit(29))
                watch = main.qobuz_volume_watch
                watch._schedule_drain()
                remote_task = watch._drain_task
                await started.wait()

                switch_task = asyncio.create_task(owner_action())
                await asyncio.sleep(0)

                self.assertFalse(switch_task.done(), "source switch must block on the canonical volume commit")
                self.assertEqual(main.playback_state.current_playback_owner, "qobuz")
                self.assertEqual(main.playback_state.current_track_info.get("source"), "qobuz")
                self.assertEqual(main.playback_state.playback_context_commit_id, "commit-old")
                if extra_assert:
                    extra_assert(stage="before_release")

                release.set()
                await remote_task
                await switch_task

                self.assertEqual(main.playback_state.current_playback_owner, expect_owner)
                if expect_track_source is not None:
                    self.assertEqual(
                        (main.playback_state.current_track_info or {}).get("source"),
                        expect_track_source,
                    )
                if extra_assert:
                    extra_assert(stage="after_release")
        finally:
            main.playback_state.current_playback_owner = original_owner
            main.playback_state.current_track_info = original_track if original_track else None
            main.playback_state.playback_context_commit_id = original_commit
            translator.reset()
            main.runtime.canonical_volume_write_lock = None

    async def test_qobuz_to_local_stays_atomic(self):
        async def local_play():
            committed = type("R", (), {"committed": True, "transition_id": "commit-local", "target_rate": 44100})()
            with mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)), \
                  mock.patch.object(main, "_record_local_track_started"):
                await main._commit_coordinated_track(
                    {"source": "local", "id": "local-new", "url": "/music/new.flac"},
                    source="local", commit_token="commit-local",
                )

        await self._hold_canonical_lock_and_switch(
            owner_action=local_play, expect_owner="local", expect_track_source="local"
        )

    async def test_qobuz_to_local_with_queue_is_atomic(self):
        import playback.queue as playback_queue
        original_queue = (
            [dict(t) for t in playback_queue.queue.tracks],
            [dict(t) for t in playback_queue.queue.original],
            playback_queue.queue.index, playback_queue.queue.mode,
            playback_queue.queue.loop, playback_queue.queue.shuffle,
            playback_queue.queue.single_track_loop,
        )
        try:
            candidate = playback_queue.QueueCandidate(
                queue=[{"id": "a", "url": "/music/a.flac", "source": "local"}],
                original=[], index=0, mode="app_replace", loop=False, shuffle=False,
                single_track_loop=False,
                track={"id": "local-new", "url": "/music/new.flac", "source": "local"},
            )

            async def local_play_with_queue():
                committed = type("R", (), {"committed": True, "transition_id": "commit-local-q", "target_rate": 44100})()
                with mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)), \
                      mock.patch.object(main, "_record_local_track_started"):
                    await main._commit_coordinated_track(
                        candidate.track, source="local", commit_token="commit-local-q",
                        queue_candidate=candidate,
                    )

            def assert_queue(stage):
                if stage == "before_release":
                    self.assertEqual(playback_queue.queue.tracks, original_queue[0])
                else:
                    self.assertEqual(len(playback_queue.queue.tracks), 1)
                    self.assertEqual(playback_queue.queue.tracks[0]["id"], "a")

            await self._hold_canonical_lock_and_switch(
                owner_action=local_play_with_queue,
                expect_owner="local", expect_track_source="local",
                extra_assert=assert_queue,
            )
        finally:
            playback_queue.queue.tracks, playback_queue.queue.original, playback_queue.queue.index, \
                playback_queue.queue.mode, playback_queue.queue.loop, playback_queue.queue.shuffle, \
                playback_queue.queue.single_track_loop = original_queue

    async def test_qobuz_to_spotify_stays_atomic(self):
        async def spotify_claim():
            committed = type("R", (), {"committed": True, "transition_id": "commit-spotify"})()
            with mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)), \
                  mock.patch.object(main, "broadcast_spotify_state", new=mock.AsyncMock(return_value={})):
                await main._publish_committed_playback_owner("spotify", "commit-spotify")

        await self._hold_canonical_lock_and_switch(
            owner_action=spotify_claim, expect_owner="spotify"
        )

    async def test_qobuz_to_stop_stays_atomic(self):
        async def stop():
            fake_player = self._fake_player()
            with mock.patch.object(main.runtime, "player_instance", fake_player), \
                  mock.patch.object(main, "_playback_transition_is_active", return_value=False), \
                  mock.patch.object(main, "get_samplerate_status", return_value={}), \
                  mock.patch.object(main.samplerate, "clear_auto_policy_force_rate"):
                await main.stop_playback()

        await self._hold_canonical_lock_and_switch(
            owner_action=stop, expect_owner=None, expect_track_source=None
        )
        self.assertIsNone(main.playback_state.current_track_info)

    async def test_restore_path_is_atomic(self):
        from playback.runtime.snapshot import _RuntimeSnapshotMixin

        async def restore():
            mixin = _RuntimeSnapshotMixin()
            mixin._deps = SimpleNamespace(
                set_track_and_owner=lambda t, o: main._atomic_set_track_and_owner(t, o),
                clear_track_and_owner=lambda: main._atomic_clear_track_and_owner(),
                mark_player_state_authoritative=lambda s: None,
                queue=lambda: __import__("playback.queue", fromlist=["queue"]).queue,
                player_is_running=lambda: False,
                get_current_track_info=lambda: main.playback_state.current_track_info,
            )
            mixin._player = SimpleNamespace(state={})
            request = SimpleNamespace(
                source="local",
                target_track={"source": "local", "id": "restored", "url": "/music/restored.flac"},
            )
            await mixin.publish_restored_source(request)

        await self._hold_canonical_lock_and_switch(
            owner_action=restore, expect_owner="local", expect_track_source="local"
        )

    async def test_abort_path_is_atomic(self):
        from playback.runtime.snapshot import _RuntimeSnapshotMixin

        async def abort():
            mixin = _RuntimeSnapshotMixin()
            mixin._deps = SimpleNamespace(
                set_track_and_owner=lambda t, o: main._atomic_set_track_and_owner(t, o),
                clear_track_and_owner=lambda: main._atomic_clear_track_and_owner(),
                mark_player_state_authoritative=lambda s: None,
                queue=lambda: __import__("playback.queue", fromlist=["queue"]).queue,
                player_is_running=lambda: False,
            )
            mixin._player = SimpleNamespace(
                state={}, set_pause=mock.Mock(), set_volume=mock.Mock(), stop_playback=mock.Mock()
            )
            await mixin._stop_staged_target_and_invalidate()

        await self._hold_canonical_lock_and_switch(
            owner_action=abort, expect_owner=None, expect_track_source=None
        )
        self.assertIsNone(main.playback_state.current_track_info)


if __name__ == "__main__":
    unittest.main()
