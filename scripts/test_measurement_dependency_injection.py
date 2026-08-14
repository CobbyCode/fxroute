#!/usr/bin/env python3
"""Measurement session resolves all application dependencies through injection.

Proves measurement_session.py no longer imports main.py: the player, the
native DSP runtime, captured-playback state and the playback orchestration
callbacks are all read through the injected ``MeasurementServices`` accessors.
This module imports only ``measurement_session`` (plus the stdlib); it never
imports ``main``, and ``measurement_session`` must not pull ``main`` into
``sys.modules``.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement_session


def subwoofer_overview():
    return {
        "selected_output": {"key": "mock", "channels": 4,
                            "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": "subwoofer-2.1",
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": 2.5,
                "sub_level_db": -3.0,
                "sub_polarity": "normal",
            },
        },
    }


class _FakePlayer:
    _running = True
    state = {
        "current_file": "/music/injected.flac",
        "ended": False,
        "paused": False,
        "position": 12.5,
    }


class _FakeDSPRuntime:
    def snapshot(self):
        return {"active": True, "helper_pid": 7777}


class _FakeSession:
    _playback_captured = False


def _services(**overrides):
    fields = {
        "get_store": lambda: None,
        "get_session": lambda: _FakeSession(),
        "auto_sub_active": lambda: False,
        "get_dsp_runtime": lambda: None,
        "get_player": lambda: None,
        "get_samplerate_status": lambda: {},
        "get_audio_output_overview": lambda: {},
        "get_current_track_info": lambda: None,
        "get_playback_transition_coordinator": lambda: None,
        "get_dsp_orchestrator": lambda: None,
        "get_playback_intent_generation": lambda: 0,
        "run_coordinated_transition": lambda *a, **k: None,
        "coordinator_current_playback_context": lambda: None,
        "begin_playback_transition_attempt": lambda: 0,
        "end_playback_transition_attempt": lambda: None,
        "get_current_pipewire_force_rate": lambda: None,
        "set_pipewire_force_rate": lambda *a, **k: None,
        "ensure_playback_samplerate_force": lambda *a, **k: None,
        "wait_for_samplerate_alignment": lambda *a, **k: None,
        "reconcile_transition_sink_rate": lambda *a, **k: None,
        "playback_graph_diagnosis": lambda *a, **k: None,
        "log_playback_graph_diagnosis": lambda *a, **k: None,
        "measurement_restore_intent_matches_live_state": lambda *a, **k: None,
        "spotify_snapshot_identity_values": lambda *a, **k: set(),
        "spotify_target_track_from_state": lambda *a, **k: {},
        "get_player_audio_samplerate": lambda *a, **k: None,
        "pulse_suspend_sink_for_samplerate": lambda *a, **k: None,
        "audio_output_overview_with_effective_rate": lambda *a, **k: {},
        "spotify_prearm_sample_rate_hz": 44100,
        "pipewire_handoff_poll_interval_ms": 50,
    }
    fields.update(overrides)
    return measurement_session.MeasurementServices(**fields)


class MeasurementDependencyInjectionTests(unittest.IsolatedAsyncioTestCase):
    def test_import_does_not_import_main(self):
        self.assertNotIn("main", sys.modules)

    def test_capture_uses_injected_player_and_intent(self):
        player = _FakePlayer()
        measurement_session.configure_services(_services(
            get_player=lambda: player,
            get_current_track_info=lambda: {
                "source": "local",
                "url": "/music/injected.flac",
                "path": "/music/injected.flac",
                "id": "t-injected",
                "title": "Injected",
                "sample_rate_hz": 44100,
            },
            get_playback_intent_generation=lambda: 42,
        ))
        measurement_session._playback_state_before_measurement = None

        measurement_session._capture_playback_state_before_measurement()

        snapshot = measurement_session._playback_state_before_measurement
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["current_file"], "/music/injected.flac")
        self.assertEqual(snapshot["id"], "t-injected")
        self.assertEqual(snapshot["intent_generation"], 42)
        self.assertEqual(snapshot["position"], 12.5)

    def test_build_context_uses_injected_dsp_runtime(self):
        fake_dsp = _FakeDSPRuntime()
        measurement_session.configure_services(_services(
            get_dsp_runtime=lambda: fake_dsp,
            get_audio_output_overview=lambda: subwoofer_overview(),
        ))
        context = measurement_session._build_measurement_audio_output_context()
        self.assertTrue(context["runtime_active"])
        self.assertEqual(context["helper_pid"], 7777)
        self.assertEqual(context["output_mode"], "subwoofer-2.1")

    async def test_restore_intent_uses_injected_callbacks(self):
        matcher = AsyncMock(return_value=True)
        identities = lambda *a, **k: {"spotify:id"}
        measurement_session.configure_services(_services(
            measurement_restore_intent_matches_live_state=matcher,
            spotify_snapshot_identity_values=identities,
        ))
        snapshot = {
            "source": "local",
            "id": "t1",
            "url": "/music/a.flac",
            "path": "/music/a.flac",
            "current_file": "/music/a.flac",
            "track_info": {"id": "t1", "url": "/music/a.flac"},
            "intent_generation": 7,
        }
        result = await measurement_session._measurement_restore_snapshot_matches_current_intent(snapshot)
        self.assertTrue(result)
        matcher.assert_awaited_once()
        self.assertEqual(matcher.await_args.kwargs["intent_generation"], 7)
        self.assertEqual(matcher.await_args.kwargs["expected_spotify_identities"], {"spotify:id"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
