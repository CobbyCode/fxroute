"""Focused tests for the provider-neutral playback owner model.

Covers the central source policy (source vs engine, graph identity) and the
authoritative ``current_playback_owner`` semantics: pause preserves the owner,
a metadata/status read never mutates it, and global transport actions route to
the owning source's adapter.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playback.source_policy as source_policy


class SourcePolicyTests(unittest.TestCase):
    def test_engine_mapping(self):
        self.assertEqual(source_policy.engine_for("local"), "mpv")
        self.assertEqual(source_policy.engine_for("radio"), "mpv")
        self.assertEqual(source_policy.engine_for("tidal"), "mpv")
        self.assertEqual(source_policy.engine_for("spotify"), "spotify")
        self.assertEqual(source_policy.engine_for("qobuz"), "qbzd")
        self.assertIsNone(source_policy.engine_for("unknown"))

    def test_classification(self):
        self.assertTrue(source_policy.is_mpv_source("tidal"))
        self.assertFalse(source_policy.is_mpv_source("spotify"))
        self.assertFalse(source_policy.is_mpv_source("qobuz"))
        self.assertTrue(source_policy.is_external_source("qobuz"))
        self.assertTrue(source_policy.is_external_source("spotify"))
        self.assertFalse(source_policy.is_external_source("radio"))
        self.assertTrue(source_policy.is_known_source("local"))
        self.assertFalse(source_policy.is_known_source(None))

    def test_graph_ports(self):
        self.assertEqual(
            source_policy.graph_port_names("spotify"),
            ("spotify:output_FL", "spotify:output_FR"),
        )
        self.assertEqual(
            source_policy.graph_port_names("tidal"),
            ("mpv:output_FL", "mpv:output_FR"),
        )
        self.assertEqual(
            source_policy.graph_port_names("qobuz"),
            ("alsa_playback.qbzd:output_FL", "alsa_playback.qbzd:output_FR"),
        )
        self.assertIsNone(source_policy.graph_port_names(None))


class QobuzReleaseBudgetTests(unittest.TestCase):
    def test_qobuz_release_timeout_exceeds_generic_handoff_timeout(self):
        import playback.media_readiness as media_readiness
        # qbzd fully disconnects its ALSA stream on pause instead of corking
        # it like Spotify Desktop (live-measured ~2.2 s), so it needs a
        # dedicated, longer bounded release budget than the 1800 ms Spotify
        # handoff timeout.
        self.assertGreater(
            media_readiness.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS,
            media_readiness.PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS,
        )
        self.assertGreaterEqual(media_readiness.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS, 4000)

    def test_qobuz_release_wait_uses_qobuz_timeout_by_default(self):
        import inspect
        import playback.media_readiness as media_readiness
        signature = inspect.signature(media_readiness.wait_for_pipewire_qobuz_release)
        self.assertEqual(
            signature.parameters["timeout_ms"].default,
            media_readiness.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS,
        )


class PlaybackOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.main = main
        import copy
        self._orig_state = copy.deepcopy(main.playback_state.__dict__)
        self._orig_lock = main.runtime.source_transition_lock
        from audio.source_feed import SourceTransitionLock
        main.runtime.source_transition_lock = SourceTransitionLock()
        main.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        self.main.playback_state.__dict__.clear()
        self.main.playback_state.__dict__.update(self._orig_state)
        self.main.runtime.source_transition_lock = self._orig_lock

    async def test_pause_preserves_owner(self):
        self.main._set_playback_owner("qobuz")
        # A paused owner is still the owner: resume/global controls stay
        # unambiguous.
        self.assertEqual(self.main._resolve_playback_owner(), "qobuz")
        self.assertEqual(self.main.playback_state.current_playback_owner, "qobuz")

    async def test_metadata_refresh_does_not_change_owner(self):
        self.main._set_playback_owner("tidal")
        self.main._resolve_playback_owner()
        self.assertEqual(self.main.playback_state.current_playback_owner, "tidal")

    async def test_readonly_derivation_never_persists(self):
        self.main._set_playback_owner(None)
        with mock.patch.object(
            self.main.runtime, "player_instance", mock.Mock(state={
                "current_file": "/music/x.flac", "playing": True, "paused": False, "ended": False,
            }),
        ), mock.patch.object(
            self.main.playback_state, "current_track_info", {"source": "local", "url": "/music/x.flac"}
        ), mock.patch.object(self.main.playback_state, "latest_spotify_state", None), mock.patch.object(
            self.main.playback_state, "latest_qobuz_state", None
        ):
            derived = self.main._derive_playback_owner_readonly()
            self.assertEqual(derived, "local")
            # Derivation is read-only; the committed owner stays None.
            self.assertIsNone(self.main.playback_state.current_playback_owner)

    async def test_route_global_control_by_owner(self):
        with mock.patch.object(
            self.main, "_spotify_global_control", new=mock.AsyncMock(return_value={"routed": "spotify"})
        ) as spot, mock.patch.object(
            self.main, "_qobuz_global_control", new=mock.AsyncMock(return_value={"routed": "qobuz"})
        ) as qob:
            self.main._set_playback_owner("spotify")
            self.assertEqual(await self.main._route_global_control("toggle"), {"routed": "spotify"})
            spot.assert_awaited_once()

            self.main._set_playback_owner("qobuz")
            self.assertEqual(await self.main._route_global_control("next"), {"routed": "qobuz"})
            qob.assert_awaited_once()

            # Native MPV sources fall through to the native transport path.
            self.main._set_playback_owner("tidal")
            self.assertIsNone(await self.main._route_global_control("toggle"))
            self.assertIsNone(await self.main._route_global_control("seek"))

    async def test_commit_coordinated_track_sets_owner(self):
        with mock.patch.object(self.main, "_mark_player_state_authoritative"), mock.patch.object(
            self.main.runtime, "player_instance", mock.Mock(state={})
        ):
            self.main._commit_coordinated_track(
                {"source": "tidal", "id": "1", "url": "https://x/stream"}, source="tidal"
            )
            self.assertEqual(self.main.playback_state.current_playback_owner, "tidal")

    async def test_publish_committed_playback_owner_advances_intent(self):
        # A committed owner change is a playback intent: queued external
        # claims of the other provider must be invalidated by the generation
        # advance so a stale claim cannot resume over the newer context.
        before = self.main.playback_state.playback_intent_generation
        with mock.patch.object(self.main.manager, "broadcast", new=mock.AsyncMock()), mock.patch.object(
            self.main, "build_playback_payload", return_value={}
        ), mock.patch.object(
            self.main.runtime, "player_instance", mock.Mock(state={})
        ):
            await self.main._publish_committed_playback_owner("qobuz", "tr-1")
        self.assertEqual(self.main.playback_state.current_playback_owner, "qobuz")
        self.assertEqual(
            self.main.playback_state.playback_intent_generation, before + 1
        )

    async def test_connect_claim_leaves_line_before_graph_and_holds_source_lock(self):
        import copy
        from audio.source_feed import SourceTransitionLock
        from types import SimpleNamespace

        for source in ("qobuz", "spotify"):
            with self.subTest(source=source):
                snapshot = copy.deepcopy(self.main.playback_state.__dict__)
                lock = SourceTransitionLock()
                selection = {"mode": "bluetooth-input"}
                playing = {"available": True, "status": "Playing", "trackId": "123", "sample_rate": 44100}
                events = []

                def select(mode, key):
                    selection.update(mode=mode)
                    return dict(selection)

                async def route(overview):
                    events.append(("route", overview["mode"]))
                    return overview

                async def run(request):
                    self.assertTrue(lock.locked(), "line monitors must wait through the graph claim")
                    self.assertEqual(selection["mode"], "app-playback", "claim must verify the App graph")
                    events.append(("claim", request.source))
                    return SimpleNamespace(committed=True, transition_id="test-claim")

                async def publish(owner, token):
                    self.assertTrue(lock.locked(), "owner publication must share source ownership")
                    self.main.playback_state.current_playback_owner = owner

                self.main.playback_state.current_playback_owner = None
                self.main.playback_state.source_mode = "bluetooth-input"
                try:
                    with mock.patch.object(self.main.runtime, "source_transition_lock", lock), mock.patch.object(
                        self.main.samplerate, "_load_audio_source_selection", side_effect=lambda: dict(selection)
                    ), mock.patch.object(self.main, "set_audio_source_selection", side_effect=select), mock.patch.object(
                        self.main, "_restore_input_output_mode", new=mock.AsyncMock(side_effect=lambda old, new: new)
                    ), mock.patch.object(self.main.external_input, "sync", side_effect=route), mock.patch.object(
                        self.main.bluetooth_input, "sync", new=mock.AsyncMock(side_effect=lambda value: value)
                    ), mock.patch.object(self.main.bluetooth_input, "finish_bluetoothctl_actions", new=mock.AsyncMock()), mock.patch.object(
                        self.main.peak_monitor_coordinator, "sync_source_mode_state", new=mock.AsyncMock()
                    ), mock.patch.object(self.main, "get_output_service", return_value=mock.Mock(ensure_state=lambda: {}, load=lambda: {"active_mode": "stereo"})), mock.patch.object(
                        self.main, "_run_coordinated_transition", side_effect=run
                    ), mock.patch.object(self.main, "_publish_committed_playback_owner", side_effect=publish), mock.patch.object(
                        self.main, f"get_{source}_ui_state", new=mock.AsyncMock(return_value=playing)
                    ), mock.patch.object(self.main, f"broadcast_{source}_state", new=mock.AsyncMock(return_value=playing)), mock.patch.object(
                        self.main, "_qobuz_pin_unity", new=mock.AsyncMock()
                    ), mock.patch.object(self.main, "_follow_active_source_native_rate", new=mock.AsyncMock()):
                        await getattr(self.main, f"_claim_{source}_playback")("connect-test")
                    self.assertEqual(events, [("route", "app-playback"), ("claim", source)])
                    self.assertEqual(self.main.playback_state.current_playback_owner, source)
                finally:
                    self.main.playback_state.__dict__.update(snapshot)

    async def test_line_claim_teardown_covers_all_line_modes_and_app_noop(self):
        for mode in ("bluetooth-input", "external-input", "stdin-input", "app-playback"):
            with self.subTest(mode=mode), mock.patch.object(
                self.main.samplerate, "_load_audio_source_selection", return_value={"mode": mode}
            ), mock.patch.object(self.main.source_transitions, "apply_selection", new=mock.AsyncMock()) as apply:
                async with self.main._source_transition_lock():
                    changed = await self.main._leave_line_input_for_streaming_claim()
                self.assertEqual(changed, mode != "app-playback")
                if changed:
                    apply.assert_awaited_once_with("app-playback", None)
                else:
                    apply.assert_not_awaited()

    async def test_inactive_retained_owner_never_changes_line_selection(self):
        for source in ("qobuz", "spotify"):
            with self.subTest(source=source):
                self.main._set_playback_owner(source)
                with mock.patch.object(self.main, f"get_{source}_ui_state", new=mock.AsyncMock(
                    return_value={"available": True, "status": "Paused"}
                )), mock.patch.object(self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)) as leave:
                    await getattr(self.main, f"_claim_{source}_playback")()
                leave.assert_not_awaited()

    async def test_claim_waiting_for_source_lock_cannot_overtake_manual_switch(self):
        import asyncio
        from types import SimpleNamespace
        from audio.source_feed import SourceTransitionLock
        lock = SourceTransitionLock()
        for source in ("qobuz", "spotify"):
            with self.subTest(source=source), mock.patch.object(self.main.runtime, "source_transition_lock", lock), mock.patch.object(
                self.main, f"get_{source}_ui_state", new=mock.AsyncMock(return_value={"available": True, "status": "Playing"})
            ), mock.patch.object(self.main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=SimpleNamespace(committed=False, state={}))) as run, mock.patch.object(
                self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)
            ) as leave:
                self.main._set_playback_owner(None)
                await lock.acquire()
                claim = asyncio.create_task(getattr(self.main, f"_claim_{source}_playback")())
                try:
                    await asyncio.sleep(0)
                    self.main.playback_state.note_source_selection("stdin-input")
                finally:
                    lock.release()
                await claim
                run.assert_not_awaited()
                leave.assert_not_awaited()

    async def test_qobuz_claim_commits_owner_despite_readonly_derivation(self):
        # qbzd playing externally while nothing is committed: the claim must
        # COMMIT the owner instead of short-circuiting on the read-only
        # derived owner (which disappears again on pause).
        playing = {
            "available": True, "status": "Playing", "title": "T", "artist": "A",
            "album": "B", "trackId": "123", "artUrl": "http://x/c.jpg", "sample_rate": 44100,
        }
        committed = type("Result", (), {"committed": True, "transition_id": "t1"})()
        with mock.patch.object(self.main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=playing)), mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)
        ) as run, mock.patch.object(
            self.main, "broadcast_qobuz_state", new=mock.AsyncMock(return_value=playing)
        ):
            await self.main._claim_qobuz_playback("qbzd-playing")
        run.assert_awaited_once()
        self.assertEqual(self.main.playback_state.current_playback_owner, "qobuz")

    async def test_qobuz_claim_returns_line_input_selection_to_app_playback(self):
        # A Connect-started Qobuz renderer is App playback: the footer must not
        # keep a line input (Bluetooth/external/STDIN) selected after the claim.
        playing = {
            "available": True, "status": "Playing", "title": "T", "artist": "A",
            "album": "B", "trackId": "123", "artUrl": "http://x/c.jpg", "sample_rate": 44100,
        }
        committed = type("Result", (), {"committed": True, "transition_id": "t1"})()
        with mock.patch.object(self.main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=playing)), mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)
        ), mock.patch.object(
            self.main, "broadcast_qobuz_state", new=mock.AsyncMock(return_value=playing)
        ), mock.patch.object(
            self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)
        ) as save:
            await self.main._claim_qobuz_playback("qbzd-playing")
        save.assert_awaited_once()

    async def test_spotify_claim_returns_line_input_selection_to_app_playback(self):
        playing = {"available": True, "status": "Playing", "title": "T", "artist": "A", "album": "B", "trackId": "spotify:track:1"}
        committed = type("Result", (), {"committed": True, "transition_id": "t2"})()
        with mock.patch.object(self.main, "get_spotify_ui_state", new=mock.AsyncMock(return_value=playing)), mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=committed)
        ), mock.patch.object(
            self.main, "broadcast_spotify_state", new=mock.AsyncMock(return_value=playing)
        ), mock.patch.object(
            self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)
        ) as save:
            await self.main._claim_spotify_playback("spotify-claim")
        save.assert_awaited_once()

    async def test_qobuz_claim_already_owned_returns_line_input_to_app_playback(self):
        # Owner persisted from an earlier Qobuz session: the claim is a no-op,
        # but a Bluetooth selection must still return to App playback.
        self.main._set_playback_owner("qobuz")
        with mock.patch.object(self.main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value={"available": True, "status": "Playing"})), mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock()
        ) as run, mock.patch.object(
            self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)
        ) as save:
            await self.main._claim_qobuz_playback("qbzd-playing")
        run.assert_not_awaited()
        save.assert_awaited_once()

    async def test_spotify_claim_already_owned_returns_line_input_to_app_playback(self):
        self.main._set_playback_owner("spotify")
        with mock.patch.object(self.main, "get_spotify_ui_state", new=mock.AsyncMock(return_value={"available": True, "status": "Playing"})), mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock()
        ) as run, mock.patch.object(
            self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=False)
        ) as save:
            await self.main._claim_spotify_playback("spotify-claim")
        run.assert_not_awaited()
        save.assert_awaited_once()

    async def test_retained_owner_claim_resumes_renderer_paused_by_line_teardown(self):
        from types import SimpleNamespace
        playing = {"available": True, "status": "Playing", "trackId": "123", "sample_rate": 44100}
        for source in ("qobuz", "spotify"):
            with self.subTest(source=source):
                state = dict(playing)
                self.main._set_playback_owner(source)

                async def leave():
                    state["status"] = "Paused"
                    return True

                async def run(request):
                    self.assertFalse(await request.skip_if_committed_owner(), "own teardown pause must not discard takeover")
                    state["status"] = "Playing"
                    return SimpleNamespace(committed=True, transition_id="takeover")

                with mock.patch.object(self.main, f"get_{source}_ui_state", new=mock.AsyncMock(side_effect=lambda: dict(state))), mock.patch.object(
                    self.main, "_leave_line_input_for_streaming_claim", side_effect=leave
                ), mock.patch.object(self.main, "_run_coordinated_transition", side_effect=run) as transition, mock.patch.object(
                    self.main, f"broadcast_{source}_state", new=mock.AsyncMock(side_effect=lambda: dict(state))
                ), mock.patch.object(self.main, "_qobuz_pin_unity", new=mock.AsyncMock()):
                    result = await getattr(self.main, f"_claim_{source}_playback")()
                transition.assert_awaited_once()
                self.assertEqual(result["status"], "Playing")

    async def test_observed_playing_claim_survives_line_monitor_pause_while_waiting(self):
        from types import SimpleNamespace
        self.main.playback_state.source_mode = "external-input"
        self.main._set_playback_owner("qobuz")
        paused = {"available": True, "status": "Paused", "trackId": "123", "sample_rate": 44100}
        with mock.patch.object(self.main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=paused)), mock.patch.object(
            self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(self.main, "_run_coordinated_transition", new=mock.AsyncMock(
            return_value=SimpleNamespace(committed=True, transition_id="observed")
        )) as run, mock.patch.object(self.main, "broadcast_qobuz_state", new=mock.AsyncMock()), mock.patch.object(
            self.main, "_qobuz_pin_unity", new=mock.AsyncMock()
        ):
            await self.main._claim_qobuz_playback("qbzd-playing", observed_playing=True)
        run.assert_awaited_once()
        self.assertTrue(run.await_args.args[0].should_play)

    async def test_line_teardown_failure_does_not_claim_or_publish_owner(self):
        playing = {"available": True, "status": "Playing"}
        for source in ("qobuz", "spotify"):
            with self.subTest(source=source), mock.patch.object(
                self.main, f"get_{source}_ui_state", new=mock.AsyncMock(return_value=playing)
            ), mock.patch.object(self.main, "_leave_line_input_for_streaming_claim", new=mock.AsyncMock(
                side_effect=RuntimeError("line teardown failed")
            )), mock.patch.object(self.main, "_run_coordinated_transition", new=mock.AsyncMock()) as run:
                self.main._set_playback_owner(None)
                with self.assertRaisesRegex(RuntimeError, "line teardown failed"):
                    await getattr(self.main, f"_claim_{source}_playback")()
                run.assert_not_awaited()
                self.assertIsNone(self.main.playback_state.current_playback_owner)

    async def test_qobuz_claim_noop_when_already_committed(self):
        self.main._set_playback_owner("qobuz")
        with mock.patch.object(self.main, "get_qobuz_ui_state", new=mock.AsyncMock(return_value={"status": "Paused"})) as get, mock.patch.object(
            self.main, "_run_coordinated_transition", new=mock.AsyncMock()
        ) as run:
            await self.main._claim_qobuz_playback("qbzd-playing")
        run.assert_not_awaited()
        get.assert_awaited_once()
        self.assertEqual(self.main.playback_state.current_playback_owner, "qobuz")


if __name__ == "__main__":
    unittest.main()
