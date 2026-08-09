#!/usr/bin/env python3
"""Host tests for MeasurementSampleRateSession.

Validates:
- Playback capture is centralised in _start_locked
- Heartbeat reopen cancels deferred release
- Session lifecycle edge cases
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Ensure the project root is on sys.path so 'import main' works.
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
import measurement_session


# ── Mocks ────────────────────────────────────────────────────────────────────

class _TestSession:
    """Thin wrapper that drives MeasurementSampleRateSession without real I/O."""

    def __init__(self):
        # Import main lazily so mocking work.
        import main
        import measurement_session
        self._main = main
        # Replace globals needed by the session / capture code.
        self._orig_measurement_sr_session = main.measurement_sr_session
        self._orig_current_track_info = main.current_track_info
        self._orig_player_instance = main.player_instance
        self._orig_get_samplerate_status = main.get_samplerate_status
        self._orig_set_pipewire_force_rate = main._set_pipewire_force_rate
        self._orig_get_current_pipewire_force_rate = main._get_current_pipewire_force_rate
        self._orig_is_measurement_window_open = main._is_measurement_window_open
        self._orig_playback_state_before_measurement = measurement_session._playback_state_before_measurement
        self._orig_last_measurement_window_seen_at = main.last_measurement_window_seen_at
        self._orig_get_player_audio_samplerate = main._get_player_audio_samplerate
        self._orig_get_spotify_ui_state = main.get_spotify_ui_state
        self._orig_latest_spotify_state = main.latest_spotify_state
        self._orig_current_footer_owner = main.current_footer_owner
        self._orig_playback_intent_generation = main.playback_intent_generation
        self._orig_playback_transition_coordinator = main.playback_transition_coordinator
        self._orig_run_coordinated_transition = main._run_coordinated_transition
        self._orig_coordinator_current_playback_context = main._coordinator_current_playback_context

        # Mock system state
        self._force_rate = 44100
        self._window_seen_at = 0.0
        self._track_info: dict | None = None
        self._player_state: dict = {}
        self._spotify_state: dict = {}
        self._set_force_calls: list[int] = []
        self._transition_requests = []

        # Patch
        main.measurement_sr_session = main.MeasurementSampleRateSession()
        measurement_session._playback_state_before_measurement = None
        main.current_track_info = self._track_info
        main.last_measurement_window_seen_at = self._window_seen_at
        main.get_samplerate_status = self._mock_get_samplerate_status
        main._set_pipewire_force_rate = self._mock_set_pipewire_force_rate
        main._get_current_pipewire_force_rate = self._mock_get_current_pipewire_force_rate
        main._is_measurement_window_open = self._mock_is_measurement_window_open
        main._get_player_audio_samplerate = self._mock_get_player_audio_samplerate
        main.get_spotify_ui_state = self._mock_get_spotify_ui_state
        main.latest_spotify_state = None
        main.playback_intent_generation = 0
        main.player_instance = None
        main._run_coordinated_transition = self._mock_run_coordinated_transition
        main._coordinator_current_playback_context = self._mock_coordinator_current_playback_context

        self._session: main.MeasurementSampleRateSession = main.measurement_sr_session

    # ── mock helpers ──
    def _mock_get_samplerate_status(self) -> dict:
        return {"force_rate": self._force_rate, "active_rate": self._force_rate}

    def _mock_set_pipewire_force_rate(self, rate: int) -> None:
        self._set_force_calls.append(rate)
        self._force_rate = rate

    def _mock_get_current_pipewire_force_rate(self) -> int | None:
        return self._force_rate

    def _mock_get_player_audio_samplerate(self) -> int | None:
        return self._force_rate

    async def _mock_run_coordinated_transition(self, request):
        import main
        self._transition_requests.append(request)
        self._force_rate = request.target_rate
        if request.source in {"local", "radio"} and main.player_instance is not None:
            state = main.player_instance.state
            state["paused"] = True
            state["playing"] = False
        elif request.source == "spotify":
            self._spotify_state["status"] = "Playing" if request.should_play else "Paused"
        return type("_TransitionResult", (), {
            "committed": True,
            "target_rate": request.target_rate,
        })()

    async def _mock_coordinator_current_playback_context(self):
        import main
        track = dict(main.current_track_info or {})
        state = dict(main.player_instance.state if main.player_instance else {})
        spotify_state = await main.get_spotify_ui_state()
        spotify_identity = spotify_state.get("trackId") or spotify_state.get("url")
        if spotify_identity and spotify_state.get("status") in {"Playing", "Paused"}:
            return {
                "source": "spotify",
                "target_url": spotify_identity,
                "target_track": {
                    "source": "spotify",
                    "id": spotify_state.get("trackId"),
                    "url": spotify_identity,
                    "title": spotify_state.get("title"),
                    "artist": spotify_state.get("artist"),
                },
                "should_play": spotify_state.get("status") == "Playing",
                "spotify": spotify_state,
            }
        return {
            "source": track.get("source") or "local",
            "target_url": state.get("current_file"),
            "target_track": track,
            "should_play": bool(not state.get("paused") and not state.get("ended")),
            "spotify": spotify_state,
        }

    async def _mock_get_spotify_ui_state(self, data=None):
        return dict(data or self._spotify_state)

    def _mock_is_measurement_window_open(self) -> bool:
        if self._window_seen_at <= 0:
            return False
        return (time.monotonic() - self._window_seen_at) <= main.MEASUREMENT_WINDOW_TTL_SECONDS

    # ── helpers ──
    def set_window_open(self, seen: bool) -> None:
        if seen:
            self._window_seen_at = time.monotonic()
        else:
            self._window_seen_at = 0.0

    def set_track_playing(self, source: str = "local", sample_rate: int = 44100) -> None:
        import main
        main.current_track_info = {
            "source": source,
            "url": "file:///test.flac",
            "path": "/test.flac",
            "id": "track1",
            "title": "Test Track",
            "sample_rate_hz": sample_rate,
        }
        # player_instance must exist for capture to proceed
        main.player_instance = type("_FakePlayer", (), {
            "_running": True,
            "state": {
                "current_file": "file:///test.flac",
                "position": 10.0,
                "paused": False,
                "ended": False,
            },
            "set_pause": lambda self, paused: self.state.update(
                paused=bool(paused), playing=not bool(paused)
            ),
        })()

    def set_spotify_playing(self, track_id: str = "spotify:track:test") -> None:
        self._spotify_state = {
            "available": True,
            "status": "Playing",
            "trackId": track_id,
            "url": track_id,
            "title": "Spotify Track",
            "artist": "Spotify Artist",
        }

    def clear_track(self) -> None:
        import main
        main.current_track_info = None
        main.player_instance = None

    def cleanup(self) -> None:
        import main
        main.measurement_sr_session = self._orig_measurement_sr_session
        main.current_track_info = self._orig_current_track_info
        main.player_instance = self._orig_player_instance
        main.get_samplerate_status = self._orig_get_samplerate_status
        main._set_pipewire_force_rate = self._orig_set_pipewire_force_rate
        main._get_current_pipewire_force_rate = self._orig_get_current_pipewire_force_rate
        main._is_measurement_window_open = self._orig_is_measurement_window_open
        measurement_session._playback_state_before_measurement = self._orig_playback_state_before_measurement
        main.last_measurement_window_seen_at = self._orig_last_measurement_window_seen_at
        main._get_player_audio_samplerate = self._orig_get_player_audio_samplerate
        main.get_spotify_ui_state = self._orig_get_spotify_ui_state
        main.latest_spotify_state = self._orig_latest_spotify_state
        main.current_footer_owner = self._orig_current_footer_owner
        main.playback_intent_generation = self._orig_playback_intent_generation
        main.playback_transition_coordinator = self._orig_playback_transition_coordinator
        main._run_coordinated_transition = self._orig_run_coordinated_transition
        main._coordinator_current_playback_context = self._orig_coordinator_current_playback_context


# ── Test cases ────────────────────────────────────────────────────────────────

class TestCentralCapture:
    """Playback capture is centralised in _start_locked."""

    def test_capture_happens_in_start_locked(self) -> None:
        """First manual sweep triggers capture via _start_locked."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.set_track_playing(source="local", sample_rate=44100)
            assert measurement_session._playback_state_before_measurement is None

            # Register first manual job → triggers _start_locked
            gen = asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert gen >= 0
            assert ts._session.active is True
            assert ts._session._playback_captured is True
            # Capture should have saved playback state before force-rate change
            saved = measurement_session._playback_state_before_measurement
            assert saved is not None, "playback state was not captured"
            assert saved.get("source") == "local"
            assert saved.get("expected_rate") == 44100
        finally:
            ts.cleanup()

    def test_auto_sub_triggers_same_capture(self) -> None:
        """Auto Sub starts a session via register_auto_sub → same capture path."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.set_track_playing(source="radio")

            gen = asyncio.get_event_loop().run_until_complete(
                ts._session.register_auto_sub("autosub-1")
            )
            assert gen >= 0
            assert ts._session.active is True
            assert ts._session._playback_captured is True
            saved = measurement_session._playback_state_before_measurement
            assert saved is not None
            assert saved.get("source") == "radio"
        finally:
            ts.cleanup()

    def test_only_one_capture_per_session(self) -> None:
        """Second manual sweep in same session does NOT re-capture."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.set_track_playing(source="local", sample_rate=44100)

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            first_capture = id(measurement_session._playback_state_before_measurement)
            assert first_capture != id(None)

            # Change track info mid-session (simulate track change)
            main.current_track_info = {
                "source": "radio",
                "url": "http://radio.example/stream",
                "id": "radio1",
                "title": "Radio Stream",
            }
            main.player_instance = type("_FakePlayer", (), {
                "_running": True,
                "state": {
                    "current_file": "http://radio.example/stream",
                    "position": 30.0,
                    "paused": False,
                    "ended": False,
                },
                "set_pause": lambda self, paused: self.state.update(
                    paused=bool(paused), playing=not bool(paused)
                ),
            })()

            # Second job registers; session already active → capture guard prevents re-capture
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-2")
            )
            second_capture = id(measurement_session._playback_state_before_measurement)
            assert second_capture == first_capture, (
                "second registration re-captured playback (should reuse first snapshot)"
            )
            # The snapshot should still be the original (local, not radio)
            assert measurement_session._playback_state_before_measurement["source"] == "local"
        finally:
            ts.cleanup()

    def test_restore_snapshot_is_invalid_after_track_change_or_stop(self) -> None:
        """The release contract rejects a snapshot that no longer matches intent."""
        ts = _TestSession()
        try:
            import main
            ts.set_track_playing(source="local", sample_rate=44100)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            saved = measurement_session._playback_state_before_measurement
            assert saved is not None
            assert asyncio.get_event_loop().run_until_complete(
                measurement_session._measurement_restore_snapshot_matches_current_intent(saved)
            )

            main.current_track_info = {
                "source": "local",
                "url": "file:///other.flac",
                "id": "other",
            }
            main.player_instance.state["current_file"] = "file:///other.flac"
            assert not asyncio.get_event_loop().run_until_complete(
                measurement_session._measurement_restore_snapshot_matches_current_intent(saved)
            )

            main.current_track_info = None
            main.player_instance = None
            assert not asyncio.get_event_loop().run_until_complete(
                measurement_session._measurement_restore_snapshot_matches_current_intent(saved)
            )
        finally:
            ts.cleanup()

    def test_spotify_capture_uses_live_entry_context_and_restores_same_track(self) -> None:
        """Spotify A is captured from entry context, paused for measurement, then restarted."""
        ts = _TestSession()
        try:
            import main

            ts.set_spotify_playing("spotify:track:A")
            ts.set_track_playing(source="local", sample_rate=44100)
            main.player_instance.state.update(paused=True, playing=False)
            local_track_before = dict(main.current_track_info)
            local_state_before = dict(main.player_instance.state)
            # A stale cache must not become the capture authority.
            main.latest_spotify_state = {
                "available": True,
                "status": "Playing",
                "trackId": "spotify:track:B",
            }

            restore_calls = []

            class Coordinator:
                async def restore_measurement(self, **kwargs):
                    restore_calls.append(kwargs)
                    ts._spotify_state["status"] = "Playing"
                    return SimpleNamespace(committed=True)

            main.playback_transition_coordinator = Coordinator()
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("spotify-a")
            )

            saved = measurement_session._playback_state_before_measurement
            assert saved is not None
            assert saved["source"] == "spotify"
            assert saved["id"] == "spotify:track:A"
            assert saved["url"] == "spotify:track:A"
            assert ts._spotify_state["status"] == "Paused"
            assert main.current_track_info == local_track_before

            asyncio.get_event_loop().run_until_complete(ts._session.request_close())
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("spotify-a")
            )

            assert len(restore_calls) == 1
            assert restore_calls[0]["target_url"] == "spotify:track:A"
            assert restore_calls[0]["should_play"] is True
            assert restore_calls[0]["restore_intent"]["id"] == "spotify:track:A"
            assert ts._spotify_state["status"] == "Playing"
            assert main.current_track_info == local_track_before
            assert main.player_instance.state == local_state_before
        finally:
            ts.cleanup()

    def test_spotify_snapshot_is_discarded_after_external_track_change(self) -> None:
        """A snapshot for Spotify A must not resurrect Spotify after an external switch to B."""
        ts = _TestSession()
        try:
            import main

            ts.set_spotify_playing("spotify:track:A")
            ts.set_track_playing(source="local", sample_rate=44100)
            main.player_instance.state.update(paused=True, playing=False)
            local_track_before = dict(main.current_track_info)
            coordinator = SimpleNamespace(
                restore_measurement=AsyncMock(return_value=SimpleNamespace(committed=True))
            )
            main.playback_transition_coordinator = coordinator

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("spotify-a")
            )
            ts._spotify_state.update({
                "status": "Paused",
                "trackId": "spotify:track:B",
                "url": "spotify:track:B",
            })

            saved = measurement_session._playback_state_before_measurement
            assert saved is not None
            assert not asyncio.get_event_loop().run_until_complete(
                measurement_session._measurement_restore_snapshot_matches_current_intent(saved)
            )

            asyncio.get_event_loop().run_until_complete(ts._session.request_close())
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("spotify-a")
            )

            coordinator.restore_measurement.assert_not_awaited()
            assert measurement_session._playback_state_before_measurement is None
            assert main.current_track_info == local_track_before
            assert not main.current_track_info.get("source") == "spotify"
        finally:
            ts.cleanup()

    def test_spotify_runtime_restore_validation_ignores_local_mpv_context(self) -> None:
        """Runtime validation uses Spotify state, not a paused local MPV context."""
        ts = _TestSession()
        try:
            import main
            from playback_transition import TransitionRequest

            ts.set_spotify_playing("spotify:track:A")
            ts.set_track_playing(source="local", sample_rate=44100)
            main.player_instance.state.update(
                current_file="file:///local.flac",
                paused=True,
                playing=False,
            )
            local_track_before = dict(main.current_track_info)
            intent = {
                "source": "spotify",
                "id": "spotify:track:A",
                "url": "spotify:track:A",
                "path": "spotify:track:A",
                "track_info": {
                    "source": "spotify",
                    "id": "spotify:track:A",
                    "url": "spotify:track:A",
                },
                "intent_generation": main.playback_intent_generation,
            }
            request = TransitionRequest(
                operation="measurement-restore",
                source="spotify",
                target_rate=44100,
                target_url="spotify:track:A",
                target_track=intent["track_info"],
                should_play=True,
                rate_change=True,
                reload_source=True,
                restore_intent=intent,
            )
            runtime = main.FxrouteTransitionRuntime()

            ts._spotify_state["status"] = "Paused"
            assert asyncio.get_event_loop().run_until_complete(
                runtime.validate_measurement_restore_intent(request, {})
            ) is True
            assert main.current_track_info == local_track_before

            ts._spotify_state.update({
                "trackId": "spotify:track:B",
                "url": "spotify:track:B",
            })
            assert asyncio.get_event_loop().run_until_complete(
                runtime.validate_measurement_restore_intent(request, {})
            ) is False
            assert main.current_track_info == local_track_before
        finally:
            ts.cleanup()

    def test_release_forwards_position_and_intent_to_coordinator(self) -> None:
        """Measurement release restores the captured local position in one transition."""
        ts = _TestSession()
        try:
            import main
            ts.set_track_playing(source="local", sample_rate=44100)
            coordinator = SimpleNamespace(
                restore_measurement=AsyncMock(
                    return_value=SimpleNamespace(committed=True)
                )
            )
            main.playback_transition_coordinator = coordinator

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            asyncio.get_event_loop().run_until_complete(ts._session.request_close())
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )

            coordinator.restore_measurement.assert_awaited_once()
            kwargs = coordinator.restore_measurement.await_args.kwargs
            assert kwargs["restore_position"] == 10.0
            assert kwargs["restore_intent"]["intent_generation"] == 0
            assert kwargs["should_play"] is True
            assert kwargs["rate_change"] is True
            assert kwargs["reload_source"] is True
            assert isinstance(kwargs["attempt_epoch"], int)
        finally:
            ts.cleanup()

    def test_same_rate_snapshot_still_uses_coordinator_without_source_reload(self) -> None:
        """A valid 48-kHz snapshot is restored even when measurement changed no rate."""
        ts = _TestSession()
        try:
            import main

            ts._force_rate = 48000
            ts.set_track_playing(source="local", sample_rate=48000)
            coordinator = SimpleNamespace(
                restore_measurement=AsyncMock(
                    return_value=SimpleNamespace(committed=True)
                )
            )
            main.playback_transition_coordinator = coordinator

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("same-rate")
            )
            assert ts._session._rate_changed is False

            asyncio.get_event_loop().run_until_complete(ts._session.request_close())
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("same-rate")
            )

            coordinator.restore_measurement.assert_awaited_once()
            kwargs = coordinator.restore_measurement.await_args.kwargs
            assert kwargs["should_play"] is True
            assert kwargs["rate_change"] is False
            assert kwargs["reload_source"] is False
            assert kwargs["target_url"] == "file:///test.flac"
        finally:
            ts.cleanup()

    def test_new_session_resets_snapshot(self) -> None:
        """A new session discards the old snapshot and captures fresh."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.set_track_playing(source="local", sample_rate=44100)

            # Session 1
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert measurement_session._playback_state_before_measurement is not None
            assert measurement_session._playback_state_before_measurement["source"] == "local"
            gen1 = ts._session.generation

            # Force release (simulate close + job finish)
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
            assert ts._session.generation == gen1 + 1

            # Session 2 with different track
            ts.clear_track()
            ts.set_track_playing(source="radio")

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-2")
            )
            assert ts._session.active is True
            # Old snapshot should have been reset before new capture
            saved = measurement_session._playback_state_before_measurement
            assert saved is not None
            assert saved.get("source") == "radio", (
                f"expected radio source, got {saved.get('source')}"
            )
        finally:
            ts.cleanup()

    def test_no_playback_captured_flag_stays_true(self) -> None:
        """When no playback is running, _playback_captured is True (capture attempted)."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.clear_track()  # No track playing

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._session.active is True
            # _capture_playback_state_before_measurement returns early because
            # no current_track_info, but the guard in the function only checks
            # _playback_captured. Since _start_locked sets it to False before
            # calling capture, the function runs but finds no track.
            # Main point: _playback_captured should be True after the call
            # because the function sets it at the end even if no data was captured.
            # Let's verify no spurious late capture happens:
            ts.set_track_playing(source="local", sample_rate=44100)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-2")
            )
            # Since _playback_captured is True, the capture function returns early.
            # The snapshot should still be whatever it was (None from the first attempt).
            assert measurement_session._playback_state_before_measurement is None, (
                "late capture happened after initial no-playback session start"
            )
        finally:
            ts.cleanup()

    def test_force_rate_switched_to_measurement_rate(self) -> None:
        """Session starts switches force-rate from 44100 to 48000."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            ts._force_rate = 44100
            assert ts._force_rate == 44100

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._force_rate == 48000, (
                f"force-rate should be 48000, got {ts._force_rate}"
            )
            assert ts._session.original_force_rate == 44100
        finally:
            ts.cleanup()


class TestHeartbeatReopen:
    """Heartbeat reopen cancels deferred release."""

    def test_open_true_does_not_start_measurement_rate_session(self) -> None:
        """Opening the measurement window is state-only before the first sweep."""
        ts = _TestSession()
        try:
            assert ts._session.active is False
            asyncio.get_event_loop().run_until_complete(ts._session.request_open())
            assert ts._session.active is False
            assert ts._force_rate == 44100
            assert ts._main._measurement_session_blocks_playback_rate(44100) is False
        finally:
            ts.cleanup()

    def test_open_false_closes_immediately_when_idle(self) -> None:
        """Explicit open:false releases session when no job is active."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._session.active is True

            # Job finishes
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is True  # still active, no close requested

            # User closes window
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.active is False  # released immediately
        finally:
            ts.cleanup()

    def test_deferred_release_when_job_active(self) -> None:
        """Close during active job → deferred release pending."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._session.active is True

            # Close window while job is running
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.active is True  # still active
            assert ts._session.close_requested is True
            assert ts._session.deferred_release_pending is True

            # Job finishes → release happens
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
        finally:
            ts.cleanup()

    def test_watchdog_sets_close_during_active_job(self) -> None:
        """Watchdog detects heartbeat loss and triggers deferred close."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_auto_sub("autosub-1")
            )
            assert ts._session.active is True

            # Simulate heartbeat loss
            ts.set_window_open(False)
            # Directly call what the watchdog would call
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.close_requested is True
            assert ts._session.deferred_release_pending is True
            # Session still active because auto-sub job is running
            assert ts._session.active is True
        finally:
            ts.cleanup()

    def test_reopen_cancels_deferred_release(self) -> None:
        """Heartbeat returns with open:true → deferred release cancelled."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_auto_sub("autosub-1")
            )
            assert ts._session.active is True

            # Watchdog detected heartbeat loss, set deferred release
            ts.set_window_open(False)
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.close_requested is True
            assert ts._session.deferred_release_pending is True

            # Window returns (heartbeat with open:true)
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_open()
            )
            assert ts._session.close_requested is False, (
                "close_requested should be False after reopen"
            )
            assert ts._session.deferred_release_pending is False, (
                "deferred_release_pending should be False after reopen"
            )

            # Job finishes → should NOT release because close was cancelled
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_auto_sub("autosub-1")
            )
            assert ts._session.active is True, (
                "session should remain active after job finish because close was cancelled"
            )
        finally:
            ts.cleanup()

    def test_reopen_then_real_close(self) -> None:
        """After reopen cancels deferred, a real close:false later releases."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._session.active is True

            # Watchdog triggers close
            ts.set_window_open(False)
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.deferred_release_pending is True

            # Reopen
            ts.set_window_open(True)
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_open()
            )
            assert ts._session.deferred_release_pending is False

            # Real close later
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            assert ts._session.deferred_release_pending is True
            # Job finishes → release
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
        finally:
            ts.cleanup()

    def test_auto_policy_replaces_old_return_to_rate_with_zero(self) -> None:
        """Without playback, Auto releases measurement ownership to PipeWire."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            ts._force_rate = 96000

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._force_rate == 48000  # Switched to measurement rate
            assert ts._session.original_force_rate == 96000

            # Close → release
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
            assert ts._force_rate == 0, (
                f"Auto policy should release force-rate to 0, got {ts._force_rate}"
            )
        finally:
            ts.cleanup()

    def test_fixed_policy_restores_selected_rate(self) -> None:
        """Without playback, Fixed returns to the durable policy rate."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            ts._force_rate = 96000
            with patch.object(
                measurement_session.samplerate,
                "load_sample_rate_policy",
                return_value={"mode": "fixed", "rate": 96000},
            ):
                asyncio.get_event_loop().run_until_complete(
                    ts._session.register_manual_job("job-1")
                )
                asyncio.get_event_loop().run_until_complete(ts._session.request_close())
                asyncio.get_event_loop().run_until_complete(
                    ts._session.unregister_manual_job("job-1")
                )
            assert ts._force_rate == 96000
        finally:
            ts.cleanup()

    def test_force_rate_zero_restored_as_zero(self) -> None:
        """When original force-rate was 0, restore to 0."""
        ts = _TestSession()
        try:
            ts.set_window_open(True)
            ts._force_rate = 0

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._session.original_force_rate == 0

            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
            assert ts._force_rate == 0
        finally:
            ts.cleanup()

    def test_external_force_rate_change_skips_restore(self) -> None:
        """When force-rate was changed externally during session, restore is skipped."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts._force_rate = 44100

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert ts._force_rate == 48000  # Set by session
            assert ts._session.original_force_rate == 44100

            # External code changes force-rate to something else
            ts._force_rate = 88200

            # Close → restore skipped because current != measurement_rate
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
            assert ts._session.active is False
            assert ts._force_rate == 88200, (
                "force-rate should NOT have been overwritten (external change protected)"
            )
        finally:
            ts.cleanup()

    def test_release_after_rate_mismatch_completes(self) -> None:
        """A release after a rate-mismatched session completes and keeps the snapshot."""
        ts = _TestSession()
        try:
            import main
            ts.set_window_open(True)
            ts.set_track_playing(source="local", sample_rate=44100)

            asyncio.get_event_loop().run_until_complete(
                ts._session.register_manual_job("job-1")
            )
            assert measurement_session._playback_state_before_measurement is not None

            # Release
            asyncio.get_event_loop().run_until_complete(
                ts._session.request_close()
            )
            asyncio.get_event_loop().run_until_complete(
                ts._session.unregister_manual_job("job-1")
            )
        finally:
            ts.cleanup()


# ── Runner ────────────────────────────────────────────────────────────────────

def run_tests() -> int:
    """Discover and run all test methods. Returns the number of failures."""
    import traceback

    test_classes = [TestCentralCapture, TestHeartbeatReopen]
    passed = 0
    failed = 0
    errors: list[str] = []

    for cls in test_classes:
        instance = cls()
        print(f"\n{'='*60}")
        print(f"  {cls.__name__}")
        print(f"{'='*60}")
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            if not callable(method):
                continue
            try:
                method()
                passed += 1
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                err = traceback.format_exc()
                errors.append(f"  ✗ {name}\n{err}")
                print(f"  ✗ {name}")
                # Print traceback for debugging
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    if errors:
        print("\nFailures:")
        for err in errors:
            print(err)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests())
