#!/usr/bin/env python3
"""Measurement session resolves player/DSP runtime through injected accessors.

Proves the lifecycle-owned runtime back-import from main.py is gone: the
measurement capture and audio-context helpers read the player and native DSP
runtime from the injected ``MeasurementServices`` accessors and never from
``main.runtime``.  The runtime state under ``main.runtime`` is deliberately
replaced with a sentinel so any fallback to it fails the test.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
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


def _configure(*, player, dsp_runtime):
    measurement_session.configure_services(measurement_session.MeasurementServices(
        get_store=lambda: None,
        get_session=lambda: _FakeSession(),
        auto_sub_active=lambda: False,
        get_dsp_runtime=lambda: dsp_runtime,
        get_player=lambda: player,
    ))


class MeasurementRuntimeInjectionTests(unittest.TestCase):
    def setUp(self):
        self._orig_player = main.runtime.player_instance
        self._orig_dsp = main.runtime.dsp_runtime
        self._orig_track_info = main.current_track_info
        self._orig_intent_generation = main.playback_intent_generation

    def tearDown(self):
        main.runtime.player_instance = self._orig_player
        main.runtime.dsp_runtime = self._orig_dsp
        main.current_track_info = self._orig_track_info
        main.playback_intent_generation = self._orig_intent_generation

    def test_capture_uses_injected_player_not_main_runtime(self):
        fake_player = _FakePlayer()
        _configure(player=fake_player, dsp_runtime=None)
        # A sentinel on main.runtime makes any fallback to it fail loudly.
        main.runtime.player_instance = object()
        main.current_track_info = {
            "source": "local",
            "url": "/music/injected.flac",
            "path": "/music/injected.flac",
            "id": "t-injected",
            "title": "Injected",
            "sample_rate_hz": 44100,
        }
        main.playback_intent_generation = 42
        measurement_session._playback_state_before_measurement = None

        measurement_session._capture_playback_state_before_measurement()

        snapshot = measurement_session._playback_state_before_measurement
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["current_file"], "/music/injected.flac")
        self.assertEqual(snapshot["id"], "t-injected")
        self.assertEqual(snapshot["intent_generation"], 42)
        self.assertEqual(snapshot["position"], 12.5)

    def test_build_context_uses_injected_dsp_runtime_not_main_runtime(self):
        fake_dsp = _FakeDSPRuntime()
        _configure(player=None, dsp_runtime=fake_dsp)
        main.runtime.dsp_runtime = object()
        with patch.object(main, "get_audio_output_overview", return_value=subwoofer_overview()):
            context = measurement_session._build_measurement_audio_output_context()
        self.assertTrue(context["runtime_active"])
        self.assertEqual(context["helper_pid"], 7777)
        self.assertEqual(context["output_mode"], "subwoofer-2.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
