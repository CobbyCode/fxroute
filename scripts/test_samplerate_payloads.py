#!/usr/bin/env python3
"""Behavior tests for sample-rate payload helpers (REFACTOR-003).

The five stateless normalization functions moved from main.py to samplerate.py;
main.py keeps thin wrappers. Covers field priority, numeric edge cases, missing
levels, unchanged input dicts and wrapper parity.
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import measurement.session as measurement_session
import audio.samplerate as samplerate


class SampleRatePolicyTests(unittest.TestCase):
    def test_fixed_policy_overrides_source_rate_and_auto_preserves_it(self):
        self.assertEqual(
            samplerate.effective_playback_rate(44100, {"mode": "fixed", "rate": 48000}),
            48000,
        )
        self.assertEqual(
            samplerate.effective_playback_rate(44100, {"mode": "auto", "rate": None}),
            44100,
        )

    def test_policy_persistence_defaults_to_auto_and_validates_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample-rate-policy.json"
            with patch.object(samplerate.persistence, "_sample_rate_policy_path", return_value=path):
                self.assertEqual(samplerate.load_sample_rate_policy(), {"mode": "auto", "rate": None})
                samplerate.persist_sample_rate_policy({"mode": "fixed", "rate": 768000})
                self.assertEqual(samplerate.load_sample_rate_policy(), {"mode": "fixed", "rate": 768000})
                with self.assertRaises(ValueError):
                    samplerate.normalize_sample_rate_policy("fixed", 32000)


class OutputRateCapabilityTests(unittest.TestCase):
    def test_range_capability_filters_complete_candidate_list(self):
        payload = """
        Prop: key Spa:Pod:Object:Param:Format:Audio:rate (65539), flags 00000000
          Choice: type Spa:Enum:Choice:Range, flags 00000000 28 4
            Int 44100
            Int 44100
            Int 192000
        Prop: key Spa:Pod:Object:Param:Format:Audio:channels (65540), flags 00000000
        """
        self.assertEqual(
            samplerate._parse_enum_format_supported_rates(payload),
            [44100, 48000, 88200, 96000, 176400, 192000],
        )

    def test_node_inventory_maps_pipewire_id_by_sink_name(self):
        payload = '''
id 55, type PipeWire:Interface:Node/3
    node.description = "DAC"
    node.name = "alsa_output.usb-DAC"
id 56, type PipeWire:Interface:Node/3
    node.name = "fxroute_dsp_sink"
'''
        self.assertEqual(
            samplerate._parse_pw_node_ids(payload),
            {"alsa_output.usb-DAC": 55, "fxroute_dsp_sink": 56},
        )


class OverviewSampleRateTests(unittest.TestCase):
    def test_priority_output_mode_over_selected_output_over_top_level(self):
        overview = {
            "output_mode": {"effective_output_rate": 48000},
            "selected_output": {"active_rate": 44100},
            "active_rate": 96000,
        }
        self.assertEqual(samplerate.overview_sample_rate(overview), 48000)

    def test_falls_back_to_selected_output_active_rate(self):
        overview = {
            "output_mode": {"effective_output_rate": 0},
            "selected_output": {"active_rate": 44100},
            "active_rate": 96000,
        }
        self.assertEqual(samplerate.overview_sample_rate(overview), 44100)

    def test_selected_output_can_come_from_current_output(self):
        overview = {"current_output": {"active_rate": 88200}}
        self.assertEqual(samplerate.overview_sample_rate(overview), 88200)

    def test_falls_back_to_top_level_active_rate(self):
        overview = {"active_rate": 96000}
        self.assertEqual(samplerate.overview_sample_rate(overview), 96000)

    def test_only_positive_ints_are_valid(self):
        # Floats, strings, zero and negatives must not match. Note: bool is an
        # int subclass in Python, so True passes isinstance(int) and > 0 in the
        # original implementation as well (parity, not a bug).
        for invalid in (48000.0, "48000", 0, -1):
            overview = {"output_mode": {"effective_output_rate": invalid}}
            self.assertIsNone(samplerate.overview_sample_rate(overview), invalid)

    def test_missing_levels_return_none(self):
        self.assertIsNone(samplerate.overview_sample_rate({}))
        self.assertIsNone(samplerate.overview_sample_rate(None))
        self.assertIsNone(samplerate.overview_sample_rate(["not", "a", "dict"]))


class AuthoritativeSampleRateTests(unittest.TestCase):
    def test_force_rate_wins_over_active_rate(self):
        status = {"force_rate": 48000, "active_rate": 44100}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 48000)

    def test_active_rate_when_no_force_rate(self):
        status = {"active_rate": 96000}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 96000)

    def test_only_positive_ints_are_valid(self):
        for invalid in (48000.0, "48000", 0, -1):
            self.assertIsNone(samplerate.authoritative_sample_rate({"force_rate": invalid}), invalid)
            self.assertIsNone(samplerate.authoritative_sample_rate({"active_rate": invalid}), invalid)

    def test_missing_status_returns_none(self):
        self.assertIsNone(samplerate.authoritative_sample_rate({}))
        self.assertIsNone(samplerate.authoritative_sample_rate(None))
        self.assertIsNone(samplerate.authoritative_sample_rate("nope"))


class AudioOutputOverviewWithEffectiveRateTests(unittest.TestCase):
    def test_sets_rate_in_all_matching_levels(self):
        overview = {
            "output_mode": {"mode": "subwoofer-2.1"},
            "selected_output": {"key": "out1", "name": "Out 1"},
            "current_output": {"key": "out1", "name": "Out 1"},
            "extra": "kept",
        }
        result = samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertEqual(result["output_mode"]["effective_output_rate"], 48000)
        self.assertEqual(result["selected_output"]["active_rate"], 48000)
        self.assertEqual(result["current_output"]["active_rate"], 48000)
        self.assertEqual(result["extra"], "kept")
        self.assertEqual(result["output_mode"]["mode"], "subwoofer-2.1")

    def test_current_output_only_updated_when_key_matches(self):
        overview = {
            "selected_output": {"key": "out1"},
            "current_output": {"key": "out2"},
        }
        result = samplerate.audio_output_overview_with_effective_rate(overview, 44100)
        self.assertNotIn("active_rate", result["current_output"])
        self.assertEqual(result["selected_output"]["active_rate"], 44100)

    def test_input_overview_is_not_mutated(self):
        overview = {
            "output_mode": {"mode": "stereo"},
            "selected_output": {"key": "out1"},
            "current_output": {"key": "out1"},
            "nested": {"deep": [1, 2, 3]},
        }
        before = copy.deepcopy(overview)
        samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertEqual(overview, before)

    def test_missing_levels_are_created_or_kept(self):
        result = samplerate.audio_output_overview_with_effective_rate({}, 48000)
        self.assertEqual(result["output_mode"], {"effective_output_rate": 48000})
        self.assertIsNone(result["selected_output"])
        self.assertIsNone(result["current_output"])

        overview = {"selected_output": None, "current_output": None}
        result = samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertIsNone(result["selected_output"])
        self.assertIsNone(result["current_output"])


class MeasurementHelperSnapshotSummaryTests(unittest.TestCase):
    def test_full_snapshot_maps_all_fields(self):
        snapshot = {
            "active": True,
            "helper_pid": 4242,
            "config": {
                "sample_rate": 48000,
                "sub_alignment_ms": 2.0,
                "derived_main_delay_ms": 1.5,
                "derived_sub_delay_ms": 2.5,
            },
            "stage": "measuring",
            "last_error": None,
        }
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary(snapshot),
            {
                "active": True,
                "helper_pid": 4242,
                "sample_rate": 48000,
                "sub_alignment_ms": 2.0,
                "main_delay_ms": 1.5,
                "sub_delay_ms": 2.5,
                "stage": "measuring",
                "last_error": None,
            },
        )

    def test_inactive_snapshot_and_missing_config(self):
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary({"active": False}),
            {
                "active": False,
                "helper_pid": None,
                "sample_rate": None,
                "sub_alignment_ms": None,
                "main_delay_ms": None,
                "sub_delay_ms": None,
                "stage": None,
                "last_error": None,
            },
        )
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary(None)["active"],
            False,
        )


class MainWrapperParityTests(unittest.TestCase):
    def test_coordinator_target_rate_uses_persisted_policy(self):
        track = {"sample_rate_hz": 44100}
        with patch.object(
            samplerate.persistence,
            "load_sample_rate_policy",
            return_value={"mode": "fixed", "rate": 48000},
        ):
            self.assertEqual(main._coordinator_target_rate("local", track), 48000)
        with patch.object(
            samplerate.persistence,
            "load_sample_rate_policy",
            return_value={"mode": "auto", "rate": None},
        ):
            self.assertEqual(main._coordinator_target_rate("local", track), 44100)


class SampleRatePolicyTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def _capture_request(self, *, active_rate, target_rate, source="local"):
        captured = []

        async def run(request):
            captured.append(request)
            return SimpleNamespace(committed=True)

        context = {
            "source": source,
            "target_url": "/music/current.flac" if source != "spotify" else "spotify-track-1",
            "target_track": {
                "source": source,
                "url": "/music/current.flac" if source != "spotify" else "spotify-track-1",
                "sample_rate_hz": active_rate,
            },
            "should_play": True,
        }
        overview = {"selected_output": {"supported_rates": [44100, 48000]}}
        with patch.object(main, "get_audio_output_overview", return_value=overview), patch.object(
            main, "_coordinator_current_playback_context", new=AsyncMock(return_value=context)
        ), patch.object(
            main, "_get_player_audio_samplerate", return_value=active_rate
        ), patch.object(
            main, "get_samplerate_status", return_value={
                "active_rate": active_rate,
                "force_rate": active_rate,
            }
        ), patch.object(main, "_run_coordinated_transition", new=run):
            await main._transition_sample_rate_policy(
                {"mode": "fixed", "rate": target_rate}, detail="test-policy"
            )
        return captured[0]

    async def test_real_local_policy_changes_request_reload_in_both_directions(self):
        for active_rate, target_rate in ((44100, 48000), (48000, 44100)):
            request = await self._capture_request(
                active_rate=active_rate, target_rate=target_rate
            )
            self.assertTrue(request.rate_change)
            self.assertTrue(request.reload_source)

    async def test_same_rate_policy_does_not_request_reload(self):
        request = await self._capture_request(active_rate=48000, target_rate=48000)
        self.assertFalse(request.rate_change)
        self.assertFalse(request.reload_source)

    async def test_spotify_rate_change_requests_full_replay(self):
        request = await self._capture_request(
            active_rate=44100, target_rate=48000, source="spotify"
        )
        self.assertTrue(request.rate_change)
        self.assertTrue(request.reload_source)

    def test_overview_sample_rate_wrapper_matches(self):
        cases = [
            {"output_mode": {"effective_output_rate": 48000}, "active_rate": 96000},
            {"selected_output": {"active_rate": 44100}},
            {"active_rate": 88200},
            {},
            None,
            {"output_mode": {"effective_output_rate": 0}, "active_rate": 96000},
        ]
        for overview in cases:
            self.assertEqual(
                main._overview_sample_rate(overview),
                samplerate.overview_sample_rate(overview),
            )

    def test_authoritative_sample_rate_wrapper_matches(self):
        for status in ({"force_rate": 48000, "active_rate": 44100}, {"active_rate": 96000}, {}, None):
            self.assertEqual(
                main._authoritative_sample_rate(status),
                samplerate.authoritative_sample_rate(status),
            )

    def test_native_runtime_sample_rate_uses_config_only(self):
        for snapshot in (
            {"config": {"sample_rate": 48000}, "helper_args": ["--rate", "44100"]},
            {"helper_args": ["--rate", "48000"]},
            {"helper_args": ["--rate"]},
            {},
            None,
        ):
            expected = 48000 if snapshot and snapshot.get("config") else None
            self.assertEqual(main.helper_argument_sample_rate(snapshot), expected)

    def test_overview_with_rate_normalizes_output_mode(self):
        overview = {"output_mode": {"mode": "stereo"}, "selected_output": {"key": "out1"}, "current_output": {"key": "out1"}}
        result = samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertEqual(result["output_mode"]["effective_output_rate"], 48000)
        self.assertEqual(result["selected_output"]["active_rate"], 48000)
        self.assertEqual(result["current_output"]["active_rate"], 48000)

    def test_snapshot_summary_wrapper_matches(self):
        snapshot = {"active": True, "config": {"sample_rate": 48000}, "stage": "x"}
        self.assertEqual(
            measurement_session._measurement_helper_snapshot_summary(snapshot),
            samplerate.measurement_helper_snapshot_summary(snapshot),
        )
        self.assertEqual(
            measurement_session._measurement_helper_snapshot_summary(None),
            samplerate.measurement_helper_snapshot_summary(None),
        )


class AutoPolicyForceRateClearTests(unittest.IsolatedAsyncioTestCase):
    """An auto policy must not leave a leftover force-rate pin at the graph default.

    The live force-rate is the status payload's ``mode`` source (0 -> auto),
    so a leftover pin after a fixed -> auto restore keeps reporting
    ``mode=fixed`` while the persisted policy is auto.  Once the sink sits at
    the graph default under an auto policy, the pin is cleared.
    """

    def _status(self, active_rate: int, force_rate: int, default_rate: int = 44100) -> dict:
        return {
            "status": "ok",
            "available": True,
            "mode": "fixed" if force_rate else "auto",
            "policy": {"mode": "auto", "rate": None},
            "force_rate": force_rate,
            "active_rate": active_rate,
            "default_rate": default_rate,
        }

    async def test_clear_helper_writes_zero_for_auto_at_default(self):
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "auto", "rate": None}):
            self.assertTrue(
                samplerate.clear_auto_policy_force_rate(44100, status=self._status(44100, 44100))
            )
        self.assertEqual(written, [0])

    async def test_clear_helper_skips_non_default_target(self):
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "auto", "rate": None}):
            self.assertFalse(
                samplerate.clear_auto_policy_force_rate(48000, status=self._status(48000, 48000))
            )
        self.assertEqual(written, [])

    async def test_clear_helper_idle_clears_non_default_pin(self):
        # The stop path clears any leftover pin under an auto policy: with no
        # active source, a pin at a non-default rate (e.g. a high-res track
        # that just stopped) is stale and must not linger in the payload.
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "auto", "rate": None}):
            self.assertTrue(
                samplerate.clear_auto_policy_force_rate(
                    96000, status=self._status(96000, 96000), idle=True,
                )
            )
        self.assertEqual(written, [0])

    async def test_clear_helper_idle_still_respects_fixed_policy(self):
        # idle only relaxes the default-rate guard; the auto-policy gate stays.
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "fixed", "rate": 96000}):
            self.assertFalse(
                samplerate.clear_auto_policy_force_rate(
                    96000, status=self._status(96000, 96000), idle=True,
                )
            )
        self.assertEqual(written, [])

    async def test_clear_helper_skips_fixed_policy(self):
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "fixed", "rate": 44100}):
            self.assertFalse(
                samplerate.clear_auto_policy_force_rate(44100, status=self._status(44100, 44100))
            )
        self.assertEqual(written, [])

    async def test_clear_helper_in_flight_policy_wins_over_persisted(self):
        # The persisted policy is still the old fixed one while a fixed -> auto
        # policy-change transition applies its rate; the in-flight policy must
        # drive the clear decision.
        written = []
        with patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=lambda rate: written.append(rate)), \
             patch.object(samplerate.alignment, "load_sample_rate_policy", return_value={"mode": "fixed", "rate": 48000}):
            self.assertTrue(
                samplerate.clear_auto_policy_force_rate(
                    44100,
                    app_policy={"mode": "auto", "rate": None},
                    status=self._status(44100, 44100),
                )
            )
        self.assertEqual(written, [0])

    async def test_clear_helper_status_read_failure_is_safe(self):
        with patch.object(samplerate.alignment, "get_samplerate_status", side_effect=RuntimeError("no pipewire")):
            self.assertFalse(samplerate.clear_auto_policy_force_rate(44100))

    async def test_commit_sample_rate_policy_clears_force_for_auto_at_default(self):
        # The persist stage runs after the guarded commit readback (graph
        # stable at the target), so clearing the pin there is safe where a
        # mid-transition clear is not.
        import playback.runtime.output_mode as playback_runtime_output_mode
        from playback.transition import TransitionRequest
        from playback_transition_test_support import make_transition_runtime

        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="sample-rate-policy",
            source="radio",
            target_rate=44100,
            sample_rate_policy={"mode": "auto", "rate": None},
        )
        cleared = []
        with patch.object(
            playback_runtime_output_mode, "persist_sample_rate_policy",
            return_value={"mode": "auto", "rate": None},
        ) as persist, patch.object(
            samplerate, "clear_auto_policy_force_rate",
            side_effect=lambda *a, **k: cleared.append((a, k)) or True,
        ):
            result = await runtime.commit_sample_rate_policy(request)
        persist.assert_called_once_with({"mode": "auto", "rate": None})
        self.assertEqual(result["sample_rate_policy"]["mode"], "auto")
        self.assertEqual(len(cleared), 1)
        args, kwargs = cleared[0]
        self.assertEqual(args[0], 44100)
        self.assertEqual(kwargs["app_policy"], {"mode": "auto", "rate": None})

    async def test_commit_sample_rate_policy_keeps_force_for_fixed(self):
        import playback.runtime.output_mode as playback_runtime_output_mode
        from playback.transition import TransitionRequest
        from playback_transition_test_support import make_transition_runtime

        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="sample-rate-policy",
            source="radio",
            target_rate=48000,
            sample_rate_policy={"mode": "fixed", "rate": 48000},
        )
        with patch.object(
            playback_runtime_output_mode, "persist_sample_rate_policy",
            return_value={"mode": "fixed", "rate": 48000},
        ), patch.object(
            samplerate, "clear_auto_policy_force_rate", new=AsyncMock()
        ) as clear:
            result = await runtime.commit_sample_rate_policy(request)
        self.assertEqual(result["sample_rate_policy"]["mode"], "fixed")
        clear.assert_not_awaited()

class StopRouteForceRateClearTests(unittest.IsolatedAsyncioTestCase):
    """The stop route clears a leftover force-rate pin under an auto policy.

    After playback stops there is no active source, so a force-rate left by the
    last source rate is stale: the live samplerate payload would keep reporting
    the pin (and its mode summary would mismatch the auto policy).  The route
    clears it with the idle flag; fixed policies keep their pin by design.
    """

    def _player(self) -> SimpleNamespace:
        return SimpleNamespace(
            _running=True,
            stop_playback=Mock(),
            state={},
        )

    async def test_stop_route_clears_leftover_force_rate_when_auto(self) -> None:
        player = self._player()
        with patch.object(main.runtime, "player_instance", player), \
             patch.object(main, "_mark_playback_intent_changed"), \
             patch.object(main, "_mark_player_state_authoritative"), \
             patch.object(main.playback_state, "current_track_info", None), \
             patch.object(main.radio_reconnect, "reset"), \
             patch.object(main.playback_queue.queue, "reset"), \
             patch.object(main.playback_queue.queue, "reset_mpv_loop_state"), \
             patch.object(main, "get_samplerate_status", return_value={"active_rate": 96000}), \
             patch.object(samplerate, "clear_auto_policy_force_rate", return_value=True) as clear:
            result = await main.stop_playback()
        self.assertEqual(result["status"], "stopped")
        clear.assert_called_once_with(96000, status={"active_rate": 96000}, idle=True)

    async def test_stop_route_tolerates_status_read_failure(self) -> None:
        player = self._player()
        with patch.object(main.runtime, "player_instance", player), \
             patch.object(main, "_mark_playback_intent_changed"), \
             patch.object(main, "_mark_player_state_authoritative"), \
             patch.object(main.playback_state, "current_track_info", None), \
             patch.object(main.radio_reconnect, "reset"), \
             patch.object(main.playback_queue.queue, "reset"), \
             patch.object(main.playback_queue.queue, "reset_mpv_loop_state"), \
             patch.object(main, "get_samplerate_status", side_effect=RuntimeError("no pipewire")), \
             patch.object(samplerate, "clear_auto_policy_force_rate", return_value=False) as clear:
            result = await main.stop_playback()
        self.assertEqual(result["status"], "stopped")
        clear.assert_called_once_with(0, status=None, idle=True)

    def test_status_mode_follows_persisted_policy(self):
        # mode is a payload summary of the persisted policy.  A leftover
        # force-rate pin at the graph default (force_rate 44100 == default)
        # must not make an auto policy report mode=fixed.
        pw_metadata = "key:'clock.rate' value:'44100'\nkey:'clock.force-rate' value:'44100'\n"
        wpctl = (
            "id 73, name:alsa_output.test\n"
            "\t* node.name = \"alsa_output.test\"\n"
            "\t* node.description = \"Test Sink\"\n"
        )
        pactl = "73\talsa_output.test\tPipeWire\ts32le 4ch 44100Hz\tRUNNING\n"
        pw_cli = "default.clock.rate = 44100\n"
        with patch.object(samplerate.overview, "_run_command", side_effect=[pw_metadata, wpctl, pactl, pw_cli]), \
             patch.object(samplerate.overview, "load_sample_rate_policy", return_value={"mode": "auto", "rate": None}):
            status = samplerate.get_samplerate_status()
        self.assertEqual(status["mode"], "auto")
        self.assertEqual(status["force_rate"], 44100)

        with patch.object(samplerate.overview, "_run_command", side_effect=[pw_metadata, wpctl, pactl, pw_cli]), \
             patch.object(samplerate.overview, "load_sample_rate_policy", return_value={"mode": "fixed", "rate": 48000}):
            status = samplerate.get_samplerate_status()
        self.assertEqual(status["mode"], "fixed")


if __name__ == "__main__":
    unittest.main()
