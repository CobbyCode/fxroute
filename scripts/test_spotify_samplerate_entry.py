#!/usr/bin/env python3
"""Focused Spotify entry samplerate readback contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import TransitionRequest


class SpotifyEntrySamplerateTests(unittest.IsolatedAsyncioTestCase):
    def _request(self) -> TransitionRequest:
        return TransitionRequest(
            operation="spotify-play",
            source="spotify",
            target_rate=44100,
            should_play=True,
            reload_source=True,
        )

    def _patches(self, *, spotify_rate: int, hardware_rate: int = 44100):
        return (
            patch.object(main, "player_instance", SimpleNamespace(state={})),
            patch.object(
                main,
                "get_spotify_ui_state",
                new=AsyncMock(return_value={"status": "Playing", "available": True}),
            ),
            patch.object(
                main,
                "_list_spotify_sink_inputs",
                side_effect=[
                    [{"sample_rate": spotify_rate}],
                    [{"sample_rate": spotify_rate}],
                ],
            ),
            patch.object(
                main,
                "get_samplerate_status",
                return_value={"active_rate": hardware_rate, "force_rate": hardware_rate},
            ),
            patch.object(main, "_playback_graph_links_complete", new=AsyncMock(return_value=True)),
            patch.object(
                main,
                "get_audio_output_overview",
                return_value={"output_mode": {"mode": "stereo"}},
            ),
            patch.object(main, "easyeffects_manager", None),
        )

    async def test_entry_commit_reads_stable_spotify_input_and_returns_it(self):
        runtime = main.FxrouteTransitionRuntime()
        patchers = self._patches(spotify_rate=44100)
        with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4], patchers[5], patchers[6]:
            result = await runtime._verify_transition(
                self._request(),
                require_source_volume=False,
                require_effects_runtime=False,
            )

        self.assertTrue(result["committed"])
        self.assertEqual(result["spotify_stream_rate"], 44100)
        self.assertEqual(result["active_rate"], 44100)

    async def test_entry_commit_rejects_stream_rate_different_from_target(self):
        runtime = main.FxrouteTransitionRuntime()
        patchers = self._patches(spotify_rate=48000)
        with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4], patchers[5], patchers[6]:
            with self.assertRaisesRegex(RuntimeError, "at the expected rate"):
                await runtime._verify_transition(
                    self._request(),
                    require_source_volume=False,
                    require_effects_runtime=False,
                )

    async def test_transient_wrong_rate_is_ignored_until_expected_rate_is_stable(self):
        observations = iter((
            [{"id": 7, "sample_rate": 48000}],
            [{"id": 7, "sample_rate": 48000}],
            [{"id": 7, "sample_rate": 44100}],
            [{"id": 7, "sample_rate": 44100}],
        ))
        with patch.object(main, "_list_spotify_sink_inputs", side_effect=lambda: next(observations)):
            result = await main._wait_for_spotify_sink_input_samplerate(
                expected_rate=44100,
                timeout_ms=250,
            )

        self.assertEqual(result, 44100)

    async def test_expected_input_replaces_stale_preferred_input(self):
        observations = iter((
            [{"id": 7, "sample_rate": 48000}],
            [
                {"id": 7, "sample_rate": 48000},
                {"id": 9, "sample_rate": 44100},
            ],
            [
                {"id": 7, "sample_rate": 48000},
                {"id": 9, "sample_rate": 44100},
            ],
        ))
        with patch.object(main, "_list_spotify_sink_inputs", side_effect=lambda: next(observations)):
            result = await main._wait_for_spotify_sink_input_samplerate(
                expected_rate=44100,
                timeout_ms=150,
            )

        self.assertEqual(result, 44100)

    async def test_input_identity_change_resets_expected_rate_stability(self):
        observations = iter((
            [{"id": 7, "sample_rate": 44100}],
            [{"id": 8, "sample_rate": 44100}],
            [{"id": 8, "sample_rate": 44100}],
        ))
        with patch.object(main, "_list_spotify_sink_inputs", side_effect=lambda: next(observations)):
            result = await main._wait_for_spotify_sink_input_samplerate(
                expected_rate=44100,
                timeout_ms=150,
            )

        self.assertEqual(result, 44100)

    async def test_missing_input_resets_expected_rate_stability(self):
        observations = iter((
            [{"id": 7, "sample_rate": 44100}],
            [],
            [{"id": 7, "sample_rate": 44100}],
            [{"id": 7, "sample_rate": 44100}],
        ))
        with patch.object(main, "_list_spotify_sink_inputs", side_effect=lambda: next(observations)):
            result = await main._wait_for_spotify_sink_input_samplerate(
                expected_rate=44100,
                timeout_ms=200,
            )

        self.assertEqual(result, 44100)

    async def test_persistent_wrong_rate_times_out_without_accepting_it(self):
        with patch.object(
            main,
            "_list_spotify_sink_inputs",
            return_value=[{"id": 7, "sample_rate": 48000}],
        ):
            with self.assertRaisesRegex(RuntimeError, "at the expected rate"):
                await main._wait_for_spotify_sink_input_samplerate(
                    expected_rate=44100,
                    timeout_ms=0,
                )

    async def test_corked_old_input_cannot_validate_over_active_wrong_input(self):
        # The old stream has the expected rate, but is corked.  The active
        # stream is the only playable candidate and is currently wrong, so an
        # entry readback must not succeed from the old identity.
        with patch.object(
            main,
            "_list_spotify_sink_inputs",
            return_value=[
                {"id": "old", "sample_rate": 44100, "corked": True},
                {"id": "active", "sample_rate": 48000, "corked": False},
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "at the expected rate"):
                await main._wait_for_spotify_sink_input_samplerate(
                    expected_rate=44100,
                    timeout_ms=0,
                )

    async def test_corked_old_input_is_replaced_by_new_expected_identity(self):
        observations = iter((
            [
                {"id": "old", "sample_rate": 44100, "corked": True},
                {"id": "active", "sample_rate": 48000, "corked": False},
            ],
            [
                {"id": "old", "sample_rate": 44100, "corked": True},
                {"id": "active", "sample_rate": 48000, "corked": False},
            ],
            [
                {"id": "old", "sample_rate": 44100, "corked": True},
                {"id": "new", "sample_rate": 44100, "corked": False},
            ],
            [
                {"id": "old", "sample_rate": 44100, "corked": True},
                {"id": "new", "sample_rate": 44100, "corked": False},
            ],
        ))
        with patch.object(
            main,
            "_list_spotify_sink_inputs",
            side_effect=lambda: next(observations),
        ) as list_inputs:
            result = await main._wait_for_spotify_sink_input_samplerate(
                expected_rate=44100,
                timeout_ms=250,
            )

        self.assertEqual(result, 44100)
        self.assertEqual(
            list_inputs.call_count,
            4,
            "the corked old identity must not satisfy stability early",
        )

    async def test_entry_commit_rejects_stream_and_hardware_rate_disagreement(self):
        runtime = main.FxrouteTransitionRuntime()
        patchers = self._patches(spotify_rate=44100, hardware_rate=48000)
        with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4], patchers[5], patchers[6]:
            with self.assertRaisesRegex(RuntimeError, "hardware rate mismatch"):
                await runtime._verify_transition(
                    self._request(),
                    require_source_volume=False,
                    require_effects_runtime=False,
                )

    async def test_unreadable_spotify_stream_fails_entry_readback(self):
        with patch.object(main, "_list_spotify_sink_inputs", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "samplerate did not become readable"):
                await main._wait_for_spotify_sink_input_samplerate(
                    expected_rate=44100,
                    timeout_ms=0,
                )

    async def test_spotify_recovery_releases_old_sink_before_restart(self):
        runtime = main.FxrouteTransitionRuntime()
        pause = AsyncMock()
        release = AsyncMock(return_value=True)
        request = TransitionRequest(
            operation="recovery",
            source="spotify",
            target_rate=44100,
            should_play=True,
            reload_source=True,
            rate_change=True,
        )
        with patch.object(main, "player_instance", SimpleNamespace(state={})), patch.object(
            main, "spotify_pause", pause
        ), patch.object(main, "_wait_for_pipewire_spotify_release", release):
            await runtime.quiet_old_source(request)

        pause.assert_awaited_once()
        release.assert_awaited_once()

    async def test_spotify_recovery_request_is_reload_coordinated(self):
        class CoordinatorDouble:
            transition_active = False
            last_successful_commit_id = "tr-spotify-recovery"

            def recovery_context_is_current(self, context_id):
                return context_id == self.last_successful_commit_id

            async def run_recovery(self, **kwargs):
                if await kwargs["validate"]():
                    return await kwargs["execute"]()
                return None

        run = AsyncMock(return_value=SimpleNamespace(target_rate=44100))
        track = {
            "source": "spotify",
            "trackId": "spotify:track:1",
            "url": "spotify:track:1",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", CoordinatorDouble()), patch.object(
            main, "_run_coordinated_transition", run
        ), patch.object(
            main, "coordinator_last_successful_commit_id", "tr-spotify-recovery"
        ), patch.object(
            main, "get_spotify_ui_state", new=AsyncMock(
                return_value={
                    "status": "Playing",
                    "trackId": "spotify:track:1",
                    "url": "spotify:track:1",
                }
            )
        ), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ):
            await main._request_coordinated_recovery(
                track,
                "spotify-samplerate-watcher",
                reload_source=True,
                diagnosis={"signature": "spotify:48000->44100"},
            )

        request = run.await_args.args[0]
        self.assertEqual(request.operation, "recovery")
        self.assertEqual(request.source, "spotify")
        self.assertEqual(request.recovery_commit_context_id, "tr-spotify-recovery")
        self.assertTrue(request.should_play)
        self.assertTrue(request.reload_source)
        self.assertTrue(request.rate_change)


if __name__ == "__main__":
    unittest.main()
