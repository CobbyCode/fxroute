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
from playback_transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest
from playback_transition_test_support import MainCoreTransitionRuntime


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

    async def test_measurement_restore_respects_held_session_lock_and_commits(self):
        """Restore must not re-enter the measurement lock during helper sync."""
        helper = HelperDouble(active=True, rate=48000)
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        initial = {
            "ee_ports": True,
            "helper_ports": True,
            "links": {
                "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L": False,
                "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R": False,
            },
            "links_complete": False,
        }
        stable = {
            "ee_ports": True,
            "helper_ports": True,
            "links": {
                "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L": True,
                "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R": True,
            },
            "links_complete": True,
            "signature": "stable-measurement-restore",
        }
        measurement_lock = asyncio.Lock()
        lock_flags = []
        reconciler = AsyncMock()
        events = []

        async def sync_helper(*_args, _rate_lock_held=False, **_kwargs):
            lock_flags.append(_rate_lock_held)
            if not _rate_lock_held:
                async with measurement_lock:
                    return overview
            return overview

        async def post_start_reconcile(_request):
            events.append("post-start-graph-reconcile")
            return {"graph_complete": True}

        request = TransitionRequest(
            operation="measurement-restore",
            source="radio",
            target_rate=48000,
            target_url="https://radio.example/stream",
            target_track={"source": "radio", "url": "https://radio.example/stream"},
            should_play=False,
            rate_change=False,
            reload_source=False,
        )
        with patch.object(main, "measurement_sr_session", SimpleNamespace(lock=measurement_lock)), patch.object(
            main, "get_audio_output_overview", return_value=overview
        ), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ), patch.object(
            main, "_get_current_pipewire_force_rate", return_value=48000
        ), patch.object(
            main, "_playback_graph_diagnosis", new=AsyncMock(side_effect=[initial, stable])
        ), patch.object(
            main, "_wait_for_easyeffects_output_ports", new=AsyncMock(return_value=True)
        ), patch.object(
            main, "_sync_subwoofer_runtime", side_effect=sync_helper
        ), patch.object(
            main, "_coordinator_reconcile_subwoofer_links_only", reconciler
        ), patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "easyeffects_manager", None
        ):
            runtime = MainCoreTransitionRuntime(
                target_rate=48000,
                generation=main.playback_transition_epoch,
                source="radio",
                target_url="https://radio.example/stream",
                operation="measurement-restore",
                use_core=True,
                events=events,
            )
            runtime.reconcile_post_start_graph = post_start_reconcile
            coordinator = PlaybackTransitionCoordinator(
                runtime,
                gate_settle_seconds=0,
            )
            await measurement_lock.acquire()
            try:
                result = await asyncio.wait_for(
                    coordinator.restore_measurement(
                        source=request.source,
                        target_rate=request.target_rate,
                        target_url=request.target_url,
                        target_track=request.target_track,
                        should_play=request.should_play,
                        rate_change=request.rate_change,
                        reload_source=request.reload_source,
                    ),
                    timeout=0.5,
                )
            finally:
                measurement_lock.release()

        self.assertTrue(result.committed)
        self.assertEqual(lock_flags, [True])
        reconciler.assert_awaited_once()
        self.assertFalse(coordinator.gate.closed)
        self.assertIn("effects-helper-links", events)
        self.assertIn("post-start-graph-reconcile", events)
        self.assertIn("graph-readback", events)
        self.assertIn("commit-readback", events)
        self.assertIn("gate.set:False", events)
        self.assertLess(events.index("effects-helper-links"), events.index("post-start-graph-reconcile"))
        self.assertLess(events.index("post-start-graph-reconcile"), events.index("graph-readback"))
        self.assertLess(events.index("graph-readback"), events.index("commit-readback"))
        self.assertLess(events.index("commit-readback"), events.index("gate.set:False"))

    async def test_output_mode_subwoofer_sync_reconciles_before_final_readback(self):
        """A transient EE->helper loss is repaired before the mode commit readback."""
        helper = HelperDouble(active=True, rate=48000)
        link_state = {"lost": False}
        events = []

        async def diagnose(*_args, **_kwargs):
            events.append("diagnosis")
            complete = not link_state["lost"]
            return {
                "mode": "subwoofer-2.2",
                "output_key": OUTPUT_KEY,
                "ee_ports": True,
                "helper_ports": True,
                "helper_active": True,
                "helper_rate_matches": True,
                "links": {
                    "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L": complete,
                    "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R": complete,
                },
                "links_complete": complete,
                "signature": "subwoofer-2.2|complete" if complete else "subwoofer-2.2|missing",
            }

        async def sync_helper(*_args, **_kwargs):
            events.append("sync")
            link_state["lost"] = True

        async def reconcile():
            events.append("reconcile")
            link_state["lost"] = False

        request = TransitionRequest(
            operation="output-mode-switch",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            should_play=True,
            rate_change=False,
            reload_source=False,
            output_mode_target=self.overview,
            output_mode_config={"mode": "subwoofer-2.2"},
        )
        with patch.object(main, "subwoofer_runtime", helper), patch.object(
            main, "easyeffects_manager", None
        ), patch.object(
            main, "_wait_for_easyeffects_output_ports", new=AsyncMock(return_value=True)
        ), patch.object(
            main, "_playback_graph_diagnosis", new=AsyncMock(side_effect=diagnose)
        ), patch.object(main, "_sync_subwoofer_runtime", side_effect=sync_helper), patch.object(
            main, "_coordinator_reconcile_subwoofer_links_only", side_effect=reconcile
        ) as reconciler:
            result = await main._coordinator_establish_effects_and_helper(request)

        self.assertTrue(result["graph_complete"])
        self.assertTrue(result["links_reconciled"])
        reconciler.assert_awaited_once()
        self.assertEqual(events, ["diagnosis", "sync", "reconcile", "diagnosis"])

    async def test_output_mode_rollback_reconciles_subwoofer_links_before_verify(self):
        """Rollback repairs the old subwoofer links before evaluating the graph."""
        runtime = main.FxrouteTransitionRuntime()
        old_overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": OUTPUT_KEY,
            }
        }
        snapshot = {
            "output_mode_overview": old_overview,
            "output_mode_config": {"mode": "subwoofer-2.2"},
        }
        link_state = {"lost": False}
        events = []

        async def sync_helper(*_args, **_kwargs):
            events.append("sync")
            link_state["lost"] = True

        async def reconcile():
            events.append("reconcile")
            link_state["lost"] = False

        async def diagnose(*_args, **_kwargs):
            events.append("verify")
            complete = not link_state["lost"]
            return {
                "links_complete": complete,
                "signature": "old-subwoofer|complete" if complete else "old-subwoofer|missing",
            }

        request = TransitionRequest(
            operation="output-mode-switch",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            should_play=True,
            output_mode_target={"output_mode": {"mode": "stereo"}},
            output_mode_config={"mode": "stereo"},
        )
        with patch.object(main, "persist_audio_output_mode", return_value={}), patch.object(
            main, "easyeffects_manager", None
        ), patch.object(main, "_sync_subwoofer_runtime", side_effect=sync_helper), patch.object(
            main, "_coordinator_reconcile_subwoofer_links_only", side_effect=reconcile
        ) as reconciler, patch.object(
            main, "_playback_graph_diagnosis", new=AsyncMock(side_effect=diagnose)
        ):
            await runtime.rollback_output_mode_runtime(request, snapshot)

        reconciler.assert_awaited_once()
        self.assertEqual(events, ["sync", "reconcile", "verify", "verify"])
        self.assertLess(events.index("reconcile"), events.index("verify"))



class RecoveryCoordinatorDouble:
    def __init__(self, *, transition_active: bool = False):
        self._transition_active = transition_active
        self.last_result = None
        self._delegate = PlaybackTransitionCoordinator(SimpleNamespace(), gate_settle_seconds=0)

    @property
    def transition_active(self) -> bool:
        return self._transition_active

    @property
    def last_successful_commit_id(self) -> str | None:
        if getattr(self.last_result, "committed", False):
            return str(self.last_result.transition_id)
        return getattr(main, "coordinator_last_successful_commit_id", None)

    def recovery_context_is_current(self, commit_context_id: str | None) -> bool:
        return bool(
            commit_context_id
            and not self.transition_active
            and main._coordinator_commit_context_id() == commit_context_id
        )

    async def run_recovery(self, **kwargs):
        if self.transition_active:
            return None
        return await self._delegate.run_recovery(**kwargs)


class CoordinatorRecoveryRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_recovery_signature_is_deduplicated(self):
        requests = []

        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

        async def run(request):
            requests.append(request)
            return SimpleNamespace(target_rate=44100, committed=True)

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", RecoveryCoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(main, "coordinator_last_successful_commit_id", "tr-context"
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
        with patch.object(main, "playback_transition_coordinator", RecoveryCoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(main, "coordinator_last_successful_commit_id", "tr-context"
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
        self.assertEqual(requests[0].recovery_commit_context_id, "tr-context")
        self.assertEqual(requests[0].recovery_source, "local")
        self.assertEqual(requests[0].recovery_url, "/music/current.flac")

    async def test_recovery_is_discarded_when_observed_mpv_context_is_gone(self):
        class PlayerDouble:
            state = {
                "current_file": None,
                "playing": False,
                "paused": False,
                "ended": False,
            }

        run = AsyncMock()
        track = {
            "source": "radio",
            "url": "https://radio.example/live",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", RecoveryCoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "current_track_info", dict(track)), patch.object(
            main, "coordinator_last_successful_commit_id", "tr-committed"
        ), patch.object(main, "_run_coordinated_transition", run):
            await main._request_coordinated_recovery(track, "radio-watcher")

        run.assert_not_awaited()

    async def test_recovery_is_discarded_when_commit_context_changes_before_execution(self):
        class PlayerDouble:
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

        run = AsyncMock()
        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", RecoveryCoordinatorDouble()), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(
            main, "_coordinator_commit_context_id", side_effect=("tr-before", "tr-after")
        ):
            await main._request_coordinated_recovery(track, "local-watcher")

        run.assert_not_awaited()

    async def test_failed_recovery_signature_retries_same_key_then_new_commit_context(self):
        coordinator = RecoveryCoordinatorDouble()
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
            return SimpleNamespace(target_rate=44100, committed=True)

        track = {
            "source": "local",
            "url": "/music/current.flac",
            "sample_rate_hz": 44100,
        }
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "player_instance", PlayerDouble()
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(main, "coordinator_last_successful_commit_id", "tr-before"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ):
            diagnosis = {"signature": "rate:44100->48000"}
            await main._request_coordinated_recovery(track, "spotify-watcher", diagnosis=diagnosis)
            # A failed attempt must not suppress an immediate retry of the same key.
            await main._request_coordinated_recovery(track, "spotify-watcher-repeat", diagnosis=diagnosis)
            coordinator.last_result = SimpleNamespace(
                committed=True,
                transition_id="tr-later-success",
            )
            await main._request_coordinated_recovery(track, "spotify-watcher-after-commit", diagnosis=diagnosis)

        self.assertEqual(len(requests), 3)

    async def test_successful_recovery_allows_same_signature_after_its_commit(self):
        coordinator = RecoveryCoordinatorDouble()
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
        ), patch.object(main, "_run_coordinated_transition", run), patch.object(main, "coordinator_last_successful_commit_id", "tr-before"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ):
            diagnosis = {"signature": "rate:44100->48000"}
            await main._request_coordinated_recovery(track, "watcher", diagnosis=diagnosis)
            await main._request_coordinated_recovery(track, "watcher-repeat", diagnosis=diagnosis)

        self.assertEqual(len(requests), 2)

    async def test_overlapping_attempt_epochs_stay_stale_until_last_entry_finishes(self):
        class CoordinatorDouble:
            def __init__(self):
                self.calls = 0
                self.started_epochs = []
                self.first_entered = asyncio.Event()
                self.second_entered = asyncio.Event()
                self.release_second = asyncio.Event()
                self.lock = asyncio.Lock()

            async def execute(self, request):
                self.calls += 1
                self.started_epochs.append(request.attempt_epoch)
                if self.calls == 2:
                    self.second_entered.set()
                async with self.lock:
                    if self.calls == 1:
                        self.first_entered.set()
                        await asyncio.Event().wait()
                    await self.release_second.wait()
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
            main, "playback_transition_epoch", 0
        ), patch.object(main, "playback_transition_pending_attempts", 0), patch.object(
            main, "coordinator_last_successful_commit_id", None
        ):
            first = asyncio.create_task(main._run_coordinated_transition(request))
            await coordinator.first_entered.wait()
            second = asyncio.create_task(main._run_coordinated_transition(request))
            await coordinator.second_entered.wait()

            self.assertEqual(coordinator.started_epochs, [1, 2])
            self.assertEqual(main.playback_transition_epoch, 2)
            self.assertEqual(main.playback_transition_pending_attempts, 2)
            self.assertFalse(main._playback_transition_context_is_current(2))
            self.assertIsNone(main._capture_playback_transition_epoch())

            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(main.playback_transition_pending_attempts, 1)
            self.assertFalse(main._playback_transition_context_is_current(2))
            self.assertIsNone(main._capture_playback_transition_epoch())

            coordinator.release_second.set()
            result = await second
            self.assertTrue(result.committed)
            self.assertEqual(main.playback_transition_epoch, 2)
            self.assertEqual(main.playback_transition_pending_attempts, 0)
            self.assertTrue(main._playback_transition_context_is_current(2))
            self.assertEqual(main._capture_playback_transition_epoch(), 2)

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

    async def test_sample_rate_policy_reload_releases_local_and_radio_mpv_stream(self):
        for source in ("local", "radio"):
            with self.subTest(source=source):
                player = SimpleNamespace(
                    state={"current_file": "/music/current.flac"},
                    set_volume=Mock(),
                    set_pause=Mock(),
                    stop_playback=Mock(),
                )
                request = TransitionRequest(
                    operation="sample-rate-policy",
                    source=source,
                    target_rate=48000,
                    target_url="/music/current.flac",
                    should_play=True,
                    rate_change=True,
                    reload_source=True,
                )
                release = AsyncMock(return_value=True)
                with patch.object(main, "player_instance", player), patch.object(
                    main, "_player_is_running", return_value=True
                ), patch.object(
                    main, "get_spotify_ui_state",
                    new=AsyncMock(return_value={"available": True, "status": "Paused"}),
                ), patch.object(
                    main, "_wait_for_pipewire_mpv_release", release
                ):
                    await main.FxrouteTransitionRuntime().quiet_old_source(request)

                player.set_volume.assert_called_once_with(0)
                player.set_pause.assert_called_once_with(True)
                player.stop_playback.assert_called_once_with()
                release.assert_awaited_once_with()


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




    async def test_post_start_bypass_reconciles_second_generation_direct_links(self):
        """Post-start readback must heal a re-created direct-link bypass
        instead of failing the committed output-mode switch."""
        helper = HelperDouble(active=True, rate=48000, direct=True)
        request = TransitionRequest(
            operation="output-mode-switch",
            source="radio",
            target_rate=48000,
            target_url="https://radio.example/stream",
            target_track={"source": "radio", "url": "https://radio.example/stream"},
            should_play=True,
            rate_change=False,
            reload_source=False,
            output_mode_target={
                "output_mode": {
                    "mode": "subwoofer-2.2",
                    "effective_output_key": OUTPUT_KEY,
                }
            },
        )

        async def pw_link(*_args):
            return _links_text("subwoofer-2.2", direct=helper.direct)

        with patch.object(main, "_run_pw_link_command", side_effect=pw_link), patch.object(
            main, "subwoofer_runtime", helper
        ), patch.object(main, "get_audio_output_overview", return_value={
            "output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}
        }):
            result = await main._coordinator_reconcile_post_start_graph(request)

        self.assertTrue(result["graph_complete"])
        self.assertEqual(helper.reconcile_calls, 1)
        self.assertFalse(helper.direct)



if __name__ == "__main__":
    unittest.main()
