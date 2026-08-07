#!/usr/bin/env python3
"""Coordinator-owned production graph contracts.

This file retains the former shared-handoff coverage under the new ownership
contract: rate alignment is tested separately from effects/helper assembly,
and the canonical graph snapshot is the only commit predicate.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import PlaybackTransitionFailure, TransitionRequest


OUTPUT_KEY = "alsa_output.pci-0000_00_1f.3.analog-stereo"


def _links_text(mode: str, *, direct: bool = False, source: bool = True, complete: bool = True) -> str:
    lines = [
        "ee_soe_output_level:output_FL",
        "ee_soe_output_level:output_FR",
    ]
    if source:
        lines.extend([
            "mpv:output_FL -> easyeffects_sink:playback_FL",
            "mpv:output_FR -> easyeffects_sink:playback_FR",
        ])
    if mode == "stereo":
        lines.append(
            f"ee_soe_output_level:output_FL -> {OUTPUT_KEY}:playback_FL"
        )
        if complete:
            lines.append(
                f"ee_soe_output_level:output_FR -> {OUTPUT_KEY}:playback_FR"
            )
        return "\n".join(lines)

    lines.extend([
        "fxroute_21_stage1:input_L",
        "fxroute_21_stage1:input_R",
        "fxroute_21_stage1:output_1",
        "fxroute_21_stage1:output_2",
        "fxroute_21_stage1:output_3",
        "fxroute_21_stage1:output_4",
        "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L",
        "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R",
        f"fxroute_21_stage1:output_1 -> {OUTPUT_KEY}:playback_FL",
        f"fxroute_21_stage1:output_2 -> {OUTPUT_KEY}:playback_FR",
    ])
    if mode != "subwoofer-2.1":
        lines.extend([
            f"fxroute_21_stage1:output_3 -> {OUTPUT_KEY}:playback_RL",
            f"fxroute_21_stage1:output_4 -> {OUTPUT_KEY}:playback_RR",
        ])
    if direct:
        lines.extend([
            f"ee_soe_output_level:output_FL -> {OUTPUT_KEY}:playback_FL",
            f"ee_soe_output_level:output_FR -> {OUTPUT_KEY}:playback_FR",
        ])
    return "\n".join(lines)


class HelperDouble:
    def __init__(self, *, active: bool, rate: int | None, direct: bool = False):
        self.active = active
        self.rate = rate
        self.direct = direct
        self.sync_calls = 0
        self.reconcile_calls = 0

    def snapshot(self):
        return {
            "active": self.active,
            "helper_pid": 42 if self.active else None,
            "helper_args": ["--rate", str(self.rate)] if self.rate else None,
        }

    async def reclean_direct_easyeffects_links(self):
        self.reconcile_calls += 1
        self.direct = False


class CanonicalGraphTests(unittest.IsolatedAsyncioTestCase):
    async def _diagnose(
        self,
        *,
        mode: str,
        helper: HelperDouble | None = None,
        direct: bool = False,
        source: str | None = "local",
        require_source: bool = True,
        target_rate: int | None = 48000,
        complete: bool = True,
    ):
        overview = {
            "output_mode": {
                "mode": mode,
                "effective_output_key": OUTPUT_KEY,
            }
        }
        io_text = _links_text(
            mode,
            direct=direct,
            source=source is not None,
            complete=complete,
        )
        async def pw_link(*_args):
            return io_text

        with patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(main, "get_audio_output_overview", return_value=overview):
            return await main._playback_graph_diagnosis(
                overview,
                source=source,
                target_rate=target_rate,
                require_source=require_source,
            )

    async def test_stereo_requires_source_and_direct_ee_output(self):
        diagnosis = await self._diagnose(mode="stereo")
        self.assertTrue(diagnosis["links_complete"])
        missing_source = await self._diagnose(
            mode="stereo", source=None, require_source=True
        )
        self.assertFalse(missing_source["links_complete"])

    async def test_22_direct_bypass_is_invalid_but_classified_as_link_only(self):
        helper = HelperDouble(active=True, rate=48000, direct=True)
        diagnosis = await self._diagnose(
            mode="subwoofer-2.2", helper=helper, direct=True
        )
        self.assertTrue(diagnosis["bypass_only"])
        self.assertTrue(diagnosis["direct_ee_to_hw_present"])
        self.assertFalse(diagnosis["links_complete"])

    async def test_22_helper_rate_is_part_of_commit_predicate(self):
        helper = HelperDouble(active=True, rate=44100)
        diagnosis = await self._diagnose(
            mode="subwoofer-2.2", helper=helper, target_rate=48000
        )
        self.assertFalse(diagnosis["links_complete"])
        self.assertFalse(diagnosis["helper_rate_matches"])


class CoordinatorGraphAssemblyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }

    async def test_bypass_only_recovery_reconciles_links_without_full_handoff(self):
        helper = HelperDouble(active=True, rate=48000, direct=True)
        calls = {"preset": 0, "sync": 0}
        link_state = {"direct": True}

        async def pw_link(*args):
            return _links_text("subwoofer-2.2", direct=link_state["direct"])

        async def reclean():
            helper.reconcile_calls += 1
            link_state["direct"] = False

        helper.reclean_direct_easyeffects_links = reclean
        request = TransitionRequest(
            operation="graph-reconcile",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            should_play=True,
            rate_change=False,
            reload_source=False,
            graph_only=True,
        )
        with patch.object(main, "get_audio_output_overview", return_value=self.overview), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate",
            side_effect=lambda **_kwargs: calls.__setitem__("preset", calls["preset"] + 1),
        ), patch.object(
            main, "_sync_subwoofer_runtime",
            side_effect=lambda **_kwargs: calls.__setitem__("sync", calls["sync"] + 1),
        ):
            await main._coordinator_establish_effects_and_helper(request)
        self.assertEqual(helper.reconcile_calls, 1)
        self.assertEqual(calls, {"preset": 0, "sync": 0})
        self.assertFalse(link_state["direct"])

    async def test_rate_change_syncs_helper_once_and_reaches_canonical_graph(self):
        helper = HelperDouble(active=False, rate=None)
        calls = {"preset": 0, "sync": 0}

        async def sync(**_kwargs):
            calls["sync"] += 1
            helper.active = True
            helper.rate = 48000

        async def pw_link(*args):
            return _links_text("subwoofer-2.2")

        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=48000,
            target_url="/music/target.flac",
            should_play=True,
            rate_change=True,
            reload_source=True,
        )
        with patch.object(main, "get_audio_output_overview", return_value=self.overview), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate",
            side_effect=lambda **_kwargs: calls.__setitem__("preset", calls["preset"] + 1),
        ), patch.object(main, "_sync_subwoofer_runtime", side_effect=sync), patch.object(
            main, "easyeffects_manager", None
        ):
            await main._coordinator_establish_effects_and_helper(request)
        self.assertEqual(calls, {"preset": 0, "sync": 1})
        self.assertEqual(helper.reconcile_calls, 1)


class CoordinatorRecoveryRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_recovery_signature_is_deduplicated(self):
        requests = []

        class CoordinatorDouble:
            transition_active = False

        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

        async def run(request):
            requests.append(request)
            return SimpleNamespace(target_rate=44100)

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", CoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(
            main, "coordinator_recovery_lock", None
        ), patch.object(main, "coordinator_recovery_inflight_signature", None), patch.object(
            main, "coordinator_recovery_last_signature", None
        ), patch.object(main, "coordinator_last_successful_commit_id", None
        ), patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }):
            diagnosis = {"signature": "2.2|bypass"}
            await main._request_coordinated_recovery(
                track, "watcher", graph_only=True, diagnosis=diagnosis
            )
            await main._request_coordinated_recovery(
                track, "watcher-repeat", graph_only=True, diagnosis=diagnosis
            )

        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].graph_only)
        self.assertFalse(requests[0].rate_change)
        self.assertFalse(requests[0].reload_source)

    async def test_recovery_preserves_actual_paused_state(self):
        requests = []

        class CoordinatorDouble:
            transition_active = False

        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": False,
                "paused": True,
                "ended": False,
            }

        async def run(request):
            requests.append(request)
            return SimpleNamespace(target_rate=44100)

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", CoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(
            main, "coordinator_recovery_lock", None
        ), patch.object(main, "coordinator_recovery_inflight_signature", None), patch.object(
            main, "coordinator_recovery_last_signature", None
        ), patch.object(main, "coordinator_last_successful_commit_id", None
        ), patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 44100,
            "force_rate": 44100,
        }):
            await main._request_coordinated_recovery(
                track, "watcher", diagnosis={"signature": "2.2|missing"}
            )

        self.assertEqual(len(requests), 1)
        self.assertFalse(requests[0].should_play)
        self.assertFalse(requests[0].rate_change)
        self.assertTrue(requests[0].reload_source is False)

    async def test_failed_recovery_signature_retries_after_new_successful_commit_context(self):
        class CoordinatorDouble:
            transition_active = False

        coordinator = CoordinatorDouble()
        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

        requests = []

        async def run(request):
            requests.append(request)
            if len(requests) == 1:
                raise PlaybackTransitionFailure(
                    "recovery failed", transition_id="tr-failed", stage="commit-readback"
                )
            return SimpleNamespace(target_rate=44100)

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(
            main, "coordinator_recovery_lock", None
        ), patch.object(main, "coordinator_recovery_inflight_signature", None), patch.object(
            main, "coordinator_recovery_last_signature", None
        ), patch.object(main, "coordinator_last_successful_commit_id", "tr-before"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ):
            diagnosis = {"signature": "rate:44100->48000"}
            await main._request_coordinated_recovery(track, "spotify-watcher", diagnosis=diagnosis)
            await main._request_coordinated_recovery(track, "spotify-watcher-repeat", diagnosis=diagnosis)
            coordinator.last_result = SimpleNamespace(
                committed=True,
                transition_id="tr-later-success",
            )
            await main._request_coordinated_recovery(track, "spotify-watcher-after-commit", diagnosis=diagnosis)

        self.assertEqual(len(requests), 2)

    async def test_successful_recovery_allows_same_signature_after_its_commit(self):
        class CoordinatorDouble:
            transition_active = False
            last_result = None

        coordinator = CoordinatorDouble()
        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

        requests = []

        async def run(request):
            requests.append(request)
            transition_id = f"tr-success-{len(requests)}"
            coordinator.last_result = SimpleNamespace(
                committed=True,
                transition_id=transition_id,
            )
            return SimpleNamespace(
                target_rate=44100,
                committed=True,
                transition_id=transition_id,
            )

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(
            main, "coordinator_recovery_lock", None
        ), patch.object(main, "coordinator_recovery_inflight_signature", None), patch.object(
            main, "coordinator_recovery_last_signature", None
        ), patch.object(main, "coordinator_last_successful_commit_id", "tr-before"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ):
            diagnosis = {"signature": "rate:44100->48000"}
            await main._request_coordinated_recovery(track, "watcher", diagnosis=diagnosis)
            await main._request_coordinated_recovery(track, "watcher-repeat", diagnosis=diagnosis)

        self.assertEqual(len(requests), 2)

    async def test_outer_transition_generation_closes_on_cancel_then_success(self):
        class CoordinatorDouble:
            def __init__(self):
                self.calls = 0
                self.started_generations = []
                self.entered = asyncio.Event()

            async def execute(self, _request):
                self.calls += 1
                self.started_generations.append(main.playback_transition_generation)
                if self.calls == 1:
                    self.entered.set()
                    await asyncio.Event().wait()
                return SimpleNamespace(
                    committed=True,
                    transition_id="tr-follow-up-success",
                    target_rate=44100,
                )

        coordinator = CoordinatorDouble()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/current.flac",
            should_play=True,
            rate_change=True,
            reload_source=True,
        )
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "playback_transition_generation", 20
        ), patch.object(main, "coordinator_last_successful_commit_id", None):
            cancelled = asyncio.create_task(main._run_coordinated_transition(request))
            await coordinator.entered.wait()
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled

            self.assertEqual(main.playback_transition_generation % 2, 0)
            result = await main._run_coordinated_transition(request)

        self.assertTrue(result.committed)
        self.assertEqual(coordinator.started_generations, [21, 23])
        self.assertEqual(main.playback_transition_generation % 2, 0)

    async def test_same_rate_stereo_does_not_reload_effects_or_helper(self):
        overview = {
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        calls = {"preset": 0, "repair": 0}

        async def pw_link(*_args):
            return _links_text("stereo")

        request = TransitionRequest(
            operation="resume",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            should_play=True,
            rate_change=False,
            reload_source=False,
        )
        with patch.object(main, "get_audio_output_overview", return_value=overview), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate",
            side_effect=lambda **_kwargs: calls.__setitem__("preset", calls["preset"] + 1),
        ), patch.object(
            main, "_repair_stereo_output_links_once",
            side_effect=lambda _diagnosis: calls.__setitem__("repair", calls["repair"] + 1),
        ):
            await main._coordinator_establish_effects_and_helper(request)
        self.assertEqual(calls, {"preset": 0, "repair": 0})

    async def test_same_rate_source_switch_quiets_without_releasing_mpv_stream(self):
        player = SimpleNamespace(
            state={"current_file": "/music/old.flac"},
            set_volume=Mock(),
            set_pause=Mock(),
            stop_playback=Mock(),
        )
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=48000,
            target_url="/music/new.flac",
            should_play=True,
            rate_change=False,
            reload_source=True,
        )
        with patch.object(main, "player_instance", player), patch.object(
            main, "_player_is_running", return_value=True
        ), patch.object(
            main, "pause_spotify_for_local_playback_broadcast", new=AsyncMock()
        ), patch.object(
            main, "get_spotify_ui_state",
            new=AsyncMock(return_value={"available": True, "status": "Paused"}),
        ):
            await main.FxrouteTransitionRuntime().quiet_old_source(request)

        player.set_volume.assert_called_once_with(0)
        player.set_pause.assert_called_once_with(True)
        player.stop_playback.assert_not_called()

    async def test_same_rate_22_graph_does_not_reclean_or_rebuild_helper(self):
        helper = HelperDouble(active=True, rate=48000)
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=48000,
            target_url="/music/same-rate.flac",
            should_play=True,
            rate_change=False,
            reload_source=True,
        )

        async def pw_link(*_args):
            return _links_text("subwoofer-2.2")

        with patch.object(main, "get_audio_output_overview", return_value=overview), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "easyeffects_manager", None
        ), patch.object(
            main, "_sync_subwoofer_runtime",
            side_effect=AssertionError("same-rate helper rebuild"),
        ):
            result = await main._coordinator_establish_effects_and_helper(request)

        self.assertFalse(result["dsp_reinitialized"])
        self.assertFalse(result["helper_rebuilt"])
        self.assertFalse(result["links_reconciled"])
        self.assertEqual(helper.reconcile_calls, 0)

    async def test_rate_change_reloads_only_when_active_convolver_requires_it(self):
        helper = HelperDouble(active=False, rate=None)
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        calls = {"preset": 0, "sync": 0}

        class ConvolverManager:
            def active_preset_requires_samplerate_reload(self, _rate):
                return True

        async def sync(**_kwargs):
            calls["sync"] += 1
            helper.active = True
            helper.rate = 48000

        async def pw_link(*_args):
            return _links_text("subwoofer-2.2")

        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=48000,
            target_url="/music/target.flac",
            should_play=True,
            rate_change=True,
            reload_source=True,
        )
        with patch.object(main, "get_audio_output_overview", return_value=overview), patch.object(
            main, "_run_pw_link_command", side_effect=pw_link
        ), patch.object(main, "subwoofer_runtime", helper), patch.object(
            main,
            "_sync_easyeffects_preset_for_playback_samplerate",
            side_effect=lambda **_kwargs: calls.__setitem__("preset", calls["preset"] + 1),
        ), patch.object(main, "_sync_subwoofer_runtime", side_effect=sync), patch.object(
            main, "easyeffects_manager", ConvolverManager()
        ):
            await main._coordinator_establish_effects_and_helper(request)
        self.assertEqual(calls, {"preset": 1, "sync": 1})

    async def test_graph_only_rejects_missing_non_bypass_graph(self):
        helper = HelperDouble(active=True, rate=48000)
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        request = TransitionRequest(
            operation="graph-reconcile",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            should_play=False,
            rate_change=False,
            reload_source=False,
            graph_only=True,
        )
        with patch.object(main, "get_audio_output_overview", return_value=overview), patch.object(
            main, "_run_pw_link_command",
            side_effect=lambda *_args: _links_text("subwoofer-2.2", direct=False, complete=False),
        ), patch.object(main, "subwoofer_runtime", helper):
            with self.assertRaisesRegex(RuntimeError, "graph-only reconciliation"):
                await main._coordinator_establish_effects_and_helper(request)


if __name__ == "__main__":
    unittest.main()
