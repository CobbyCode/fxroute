#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Actual state/plan activation boundaries, input memory and ingress rollback."""

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager


class ActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.manager = DSPManager(home=path)
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(path / "state.json"), preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: {}, measurement_active=lambda: False))
        state = set_mode_routing(default_output_state(), "surround", "A",
                                 ["main_l", "main_r", "sub1", "sub2", "center", "side_l", "side_r", "rear_l", "rear_r"])
        self.state = self.service.commit(state, expected_revision=0)
        self.transitions = []

        async def transition(request):
            self.transitions.append(request)
            prepared = request.output_state_transition
            self.service.commit(prepared["candidate_state"], expected_revision=prepared["expected_revision"])
            return SimpleNamespace(committed=True)

        overview = {"selected_output": {"key": "A", "channels": 9}, "output_mode": {
            "effective_output_key": "A", "effective_output_channels": 9,
            "hardware_playback_ports": [f"playback_AUX{i}" for i in range(9)]}}
        self.input = SimpleNamespace(disable=AsyncMock(), sync=AsyncMock())
        for patcher in (
            patch.object(main, "get_output_service", return_value=self.service),
            patch.object(main, "get_audio_output_overview", return_value=overview),
            patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}),
            patch.object(main, "_require_dsp_manager", return_value=self.manager),
            patch.object(main, "_coordinator_current_playback_context", AsyncMock(return_value={"source": "local"})),
            patch.object(main, "_run_coordinated_transition", transition),
            patch.object(main.samplerate, "_load_audio_source_selection", return_value={
                "mode": "external-input", "selected_input_key": "scarlett::pair:9-10"}),
            patch.object(main, "external_input", self.input),
            patch.object(main, "_set_surround_carrier_owned", AsyncMock()),
            patch.object(main, "_sync_v2_head_after_bank_assign", AsyncMock()),
            patch.object(main, "measurement_sr_session", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def switch(self):
        return await main._apply_audio_output_state_body({"expected_revision": self.state["revision"],
            "mutation": {"kind": "switch_mode", "mode": "surround"}})

    async def test_activation_uses_eight_input_plan_and_remembers_selected_pair(self):
        response = await self.switch()
        self.assertTrue(response["live_applied"])
        head = self.service.load()
        self.assertEqual(head["active_mode"], "surround")
        self.assertEqual(head["input_modes"]["external-input:scarlett::pair:9-10"], "surround")
        target = self.transitions[0].output_state_transition["target"]
        self.assertEqual(target.config.input_count, 8)
        self.assertIn("surround 1\n", target.text)
        self.assertIn("postmatrix 7 3 1\n", target.text)

    async def test_ingress_failure_reverts_committed_mode_and_bindings(self):
        self.input.sync.side_effect = RuntimeError("decoder did not start")
        with self.assertRaisesRegex(RuntimeError, "decoder did not start"):
            await self.switch()
        head = self.service.load()
        self.assertEqual(head["active_mode"], "stereo")
        self.assertEqual(head["input_modes"], {})
        self.assertEqual(head["modes"], self.state["modes"])

    async def test_live_stdin_stream_is_resynced_after_the_mode_switch(self):
        stdin = SimpleNamespace(resync_routing=AsyncMock())
        order = []
        self.input.sync.side_effect = lambda: order.append("external")
        stdin.resync_routing.side_effect = lambda: order.append("stdin")
        with patch.object(main.runtime, "stdin_input", stdin):
            await self.switch()
        self.assertEqual(order, ["external", "stdin"])

    async def test_stdin_stream_is_resynced_after_a_reverted_switch_too(self):
        stdin = SimpleNamespace(resync_routing=AsyncMock())
        self.input.sync.side_effect = [RuntimeError("decoder did not start"), None]
        with patch.object(main.runtime, "stdin_input", stdin):
            with self.assertRaisesRegex(RuntimeError, "decoder did not start"):
                await self.switch()
        stdin.resync_routing.assert_awaited_once_with()

    async def switch_to(self, mode):
        return await main._apply_audio_output_state_body({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "switch_mode", "mode": mode}})

    def digital_input(self):
        return {"mode": "external-input",
                "selected_input": {"key": "scarlett::pair:9-10", "alsa_card": 3}}

    async def test_external_input_on_a_digital_card_owns_the_carrier_outside_surround(self):
        """The S/PDIF pair needs the borrowed carrier in Stereo and Stereo + Sub too."""
        with patch.object(main, "get_audio_source_overview", return_value=self.digital_input()), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            await self.switch_to("stereo")
        main._set_surround_carrier_owned.assert_awaited_with(True, 3)

    async def test_external_input_without_a_digital_receiver_keeps_the_playback_policy(self):
        with patch.object(main, "get_audio_source_overview", return_value=self.digital_input()), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=False):
            await self.switch_to("stereo")
        main._set_surround_carrier_owned.assert_awaited_with(False, None)

    async def test_surround_owns_the_carrier_for_any_external_card(self):
        with patch.object(main, "get_audio_source_overview", return_value=self.digital_input()), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=False):
            await self.switch()
        main._set_surround_carrier_owned.assert_awaited_with(True, None)

    async def test_app_playback_switch_releases_the_carrier_without_reading_the_sources(self):
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "app-playback"}), \
                patch.object(main, "get_audio_source_overview") as overview:
            await self.switch_to("stereo")
        main._set_surround_carrier_owned.assert_awaited_with(False, None)
        overview.assert_not_called()

    async def test_monitored_digital_input_holds_the_carrier(self):
        """The input sync keeps the carrier, so a start with it selected works too."""
        with patch.object(main, "_surround_carrier_owned", False), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            await main._hold_external_input_carrier({"alsa_card": 3})
        main._set_surround_carrier_owned.assert_awaited_with(True, 3)

    async def test_monitored_digital_input_on_another_card_moves_the_carrier(self):
        with patch.object(main, "_surround_carrier_owned", True), \
                patch.object(main, "_surround_clock_card", 5), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            await main._hold_external_input_carrier({"alsa_card": 3})
        main._set_surround_carrier_owned.assert_awaited_with(True, 3)

    async def test_a_held_carrier_is_reasserted_after_playback_moved_the_rate(self):
        """Playback start/stop move the graph rate; the held carrier has to win."""
        with patch.object(main, "_surround_carrier_owned", True), \
                patch.object(main, "_surround_clock_card", 3), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            await main._hold_external_input_carrier({"alsa_card": 3})
        main._set_surround_carrier_owned.assert_awaited_with(True, 3)

    async def test_same_mode_input_change_owns_the_carrier_without_a_mode_switch(self):
        state = self.service.load()
        state["input_modes"]["external-input:scarlett::pair:9-10"] = state["active_mode"]
        self.service.commit(state, expected_revision=state["revision"])
        with patch.object(main, "_surround_carrier_owned", False), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True), \
                patch.object(main, "get_audio_source_overview") as reader:
            await main._restore_input_output_mode({"mode": "app-playback"}, self.digital_input())
        main._set_surround_carrier_owned.assert_awaited_with(True, 3)
        self.assertEqual(self.service.load()["input_modes"]["app-playback"], state["active_mode"])
        reader.assert_not_called()

    async def test_monitored_input_without_a_digital_receiver_releases_the_carrier(self):
        with patch.object(main, "_surround_carrier_owned", False), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=False):
            await main._hold_external_input_carrier({"alsa_card": 2})
        main._set_surround_carrier_owned.assert_awaited_with(False)

    async def test_a_measurement_session_keeps_its_own_rate(self):
        with patch.object(main, "_surround_carrier_owned", False), \
                patch.object(main, "_measurement_session_owns_live_rate", return_value=True), \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            await main._hold_external_input_carrier({"alsa_card": 3})
        main._set_surround_carrier_owned.assert_not_awaited()

    async def test_input_change_away_from_the_digital_input_releases_the_carrier(self):
        with patch.object(main, "_surround_carrier_owned", True):
            await main._restore_input_output_mode(self.digital_input(), {"mode": "app-playback"})
        main._set_surround_carrier_owned.assert_awaited_with(False)
        self.input.disable.assert_awaited()

    async def test_invalid_mutation_has_no_ingress_or_clock_side_effects(self):
        with self.assertRaises(main.HTTPException) as raised:
            await main._apply_audio_output_state_body({"expected_revision": self.state["revision"],
                "mutation": {"kind": "switch_mode", "mode": "surround", "unknown": 1}})
        self.assertEqual(raised.exception.status_code, 400)
        self.input.disable.assert_not_awaited()
        main._set_surround_carrier_owned.assert_not_awaited()

    async def test_clock_policy_guard_is_rechecked_after_request_body(self):
        class Request:
            async def json(self):
                main._surround_carrier_owned = True
                return {"mode": "fixed", "rate": 44100}
        with patch.object(main, "_surround_carrier_owned", False), patch.object(
                main, "_transition_sample_rate_policy", AsyncMock()) as transition:
            with self.assertRaises(main.HTTPException) as raised:
                await main.save_audio_samplerate_policy(Request())
            self.assertEqual(raised.exception.status_code, 409)
            transition.assert_not_awaited()



class DigitalCarrierRequestTests(unittest.IsolatedAsyncioTestCase):
    """Which source owns the carrier: card with a digital receiver, or Surround."""

    def overview(self, card):
        entry = {} if card is None else {"key": "scarlett::pair:9-10", "alsa_card": card}
        return {"mode": "external-input", "selected_input": entry}

    async def request(self, overview, *, external=True, mode="stereo", receiver=True):
        with patch.object(main.alsa_ctl, "has_digital_receiver", return_value=receiver):
            return await main._digital_carrier_request(overview, external=external, mode=mode)

    async def test_a_non_external_source_never_needs_the_carrier(self):
        with patch.object(main, "get_audio_source_overview") as reader:
            self.assertEqual(await self.request(None, external=False), (False, None))
        reader.assert_not_called()

    async def test_a_digital_card_is_clocked_in_every_mode(self):
        for mode in ("stereo", "stereo-sub", "surround"):
            with self.subTest(mode=mode):
                self.assertEqual(await self.request(self.overview(3), mode=mode), (True, 3))

    async def test_a_card_without_a_receiver_only_follows_surround(self):
        self.assertEqual(await self.request(self.overview(2), receiver=False), (False, None))
        self.assertEqual(await self.request(self.overview(2), mode="surround", receiver=False), (True, None))

    async def test_a_missing_card_number_is_not_a_receiver(self):
        for card in (None, "3", True):
            with self.subTest(card=card):
                self.assertEqual(await self.request(self.overview(card)), (False, None))

    async def test_a_missing_overview_reads_the_current_selection(self):
        with patch.object(main, "get_audio_source_overview", return_value=self.overview(3)) as reader, \
                patch.object(main.alsa_ctl, "has_digital_receiver", return_value=True):
            self.assertEqual(await main._digital_carrier_request(None, external=True, mode="stereo-sub"),
                             (True, 3))
        reader.assert_called_once_with()

    async def test_an_overview_whose_source_moved_on_needs_no_carrier(self):
        self.assertEqual(await self.request({"mode": "app-playback"}), (False, None))


class CarrierClockTests(unittest.IsolatedAsyncioTestCase):
    """The input card's clock moves only on a settled 48 kHz carrier."""

    def setUp(self):
        self.calls = []
        self.rates = [{"active_rate": 48000, "force_rate": 48000}]

        def status():
            return self.rates.pop(0) if len(self.rates) > 1 else self.rates[0]

        def select(card, *, digital):
            self.calls.append(("clock", card, digital))
            return None

        async def coordinated(request):
            self.calls.append(("rate", request.target_rate))

        async def release(policy, *, detail):
            self.calls.append(("policy", detail))

        orchestration = SimpleNamespace(sample_rate_policy_request=AsyncMock(
            return_value=main.TransitionRequest(operation="policy", source="local", target_rate=44100)))
        for patcher in (
            patch.object(main, "_surround_carrier_owned", False),
            patch.object(main, "_surround_clock_card", None),
            patch.object(main, "get_samplerate_status", status),
            patch.object(main.alsa_ctl, "select_clock_source", select),
            patch.object(main, "_run_coordinated_transition", coordinated),
            patch.object(main, "_transition_sample_rate_policy", release),
            patch.object(main.samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}),
            patch.object(main.playback_orchestration, "configured", return_value=orchestration),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_clock_follows_the_receiver_after_the_rate_settles(self):
        self.rates = [{"active_rate": 44100, "force_rate": 0}, {"active_rate": 48000, "force_rate": 48000}]
        await main._set_surround_carrier_owned(True, 3)
        self.assertEqual(self.calls, [("rate", 48000), ("clock", 3, True)])
        await main._set_surround_carrier_owned(True, 3)
        self.assertEqual(self.calls[-1], ("clock", 3, True), "every sync rechecks the lock")

    async def test_release_returns_the_clock_before_the_rate_moves(self):
        await main._set_surround_carrier_owned(True, 3)
        await main._set_surround_carrier_owned(True)
        self.assertEqual(main._surround_clock_card, 3, "a card-less sync keeps the clock owner")
        await main._set_surround_carrier_owned(False)
        self.assertEqual(self.calls[-2:], [("clock", 3, False), ("policy", "digital-input-clock-release")])
        self.assertFalse(main._surround_carrier_owned)
        self.assertIsNone(main._surround_clock_card)

    async def test_switching_cards_releases_the_previous_clock(self):
        await main._set_surround_carrier_owned(True, 3)
        await main._set_surround_carrier_owned(True, 5)
        self.assertEqual(self.calls, [("clock", 3, True), ("clock", 3, False), ("clock", 5, True)])


class DigitalClockGuardTests(unittest.IsolatedAsyncioTestCase):
    """Every card gets the clock the active input needs: Internal, or the held S/PDIF."""

    def setUp(self):
        self.card_clock = {"clock": 0, "locked": True, "writes": []}
        names = ["Internal", "S/PDIF", "ADAT"]

        def select(card, *, digital, require_receiver):
            validity = {"S/PDIF Validity": self.card_clock["locked"], "Internal Validity": True}
            target = main.alsa_ctl.clock_source_target(names, self.card_clock["clock"], validity, digital=digital)
            if target is None:
                return None
            self.card_clock["clock"] = target
            self.card_clock["writes"].append(names[target])
            return names[target]

        async def policy(*_args, **_kwargs):
            return None

        orchestration = SimpleNamespace(sample_rate_policy_request=AsyncMock(
            return_value=main.TransitionRequest(operation="policy", source="local", target_rate=48000)))
        for patcher in (
            patch.object(main, "_surround_carrier_owned", False),
            patch.object(main, "_surround_clock_card", None),
            patch.object(main, "get_samplerate_status", lambda: {"active_rate": 48000, "force_rate": 48000}),
            patch.object(main.alsa_ctl, "list_cards", return_value=[3]),
            patch.object(main.alsa_ctl, "_select_clock_source", select),
            patch.object(main, "_transition_sample_rate_policy", policy),
            patch.object(main.samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}),
            patch.object(main.playback_orchestration, "configured", return_value=orchestration),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_wanted_clock_follows_the_carrier_owner(self):
        self.assertIs(main._wanted_digital_clock(3), False, "no carrier: Internal")
        with patch.object(main, "_surround_carrier_owned", True):
            self.assertIsNone(main._wanted_digital_clock(3), "carrier without a clock card: not managed")
            with patch.object(main, "_surround_clock_card", 3):
                self.assertIs(main._wanted_digital_clock(3), True, "the holder's card follows the receiver")
                self.assertIs(main._wanted_digital_clock(5), False, "another card is not held")

    async def test_clock_goes_back_to_internal_after_a_reenumeration_without_the_carrier(self):
        self.card_clock["clock"] = 1
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["writes"], ["Internal"])

    async def test_held_clock_comes_back_after_a_reenumeration(self):
        await main._set_surround_carrier_owned(True, 3)
        self.assertEqual(self.card_clock["writes"], ["S/PDIF"])
        self.card_clock["clock"] = 0  # the device re-enumerated and came back on Internal
        with self.assertLogs(main.logger, "INFO") as logged:
            await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["writes"], ["S/PDIF", "S/PDIF"])
        self.assertTrue(any("Digital carrier clock restored: ALSA card 3" in line for line in logged.output))
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["writes"], ["S/PDIF", "S/PDIF"], "a clock that is right stays")

    async def test_held_clock_waits_for_the_lock_and_adat_is_never_touched(self):
        await main._set_surround_carrier_owned(True, 3)
        self.card_clock.update(clock=0, locked=False)
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["clock"], 0, "no receiver lock: stay on Internal")
        self.card_clock["clock"] = 2
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["clock"], 2, "ADAT is the user's choice")
        await main._set_surround_carrier_owned(False)
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["clock"], 2)

    async def test_released_carrier_is_not_taken_back_by_the_guard(self):
        await main._set_surround_carrier_owned(True, 3)
        await main._set_surround_carrier_owned(False)
        self.assertEqual(self.card_clock["writes"], ["S/PDIF", "Internal"])
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["writes"], ["S/PDIF", "Internal"])
        self.card_clock["clock"] = 1  # restored from the saved state later
        await main._settle_digital_clocks()
        self.assertEqual(self.card_clock["writes"], ["S/PDIF", "Internal", "Internal"])

    async def test_guard_loop_survives_a_failing_check_and_repeats(self):
        real_sleep = asyncio.sleep
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError
            await real_sleep(0)

        check = AsyncMock(side_effect=[OSError("busy"), None, None])
        with patch.object(main, "_settle_digital_clocks", check), \
                patch.object(main.asyncio, "sleep", sleep), self.assertRaises(asyncio.CancelledError):
            await main._digital_clock_guard_loop()
        self.assertEqual(check.await_count, 3)
        self.assertEqual(delays, [main.DIGITAL_CLOCK_GUARD_SECONDS] * 3)

    def test_startup_runs_the_guard_as_a_lifecycle_task(self):
        source = inspect.getsource(main.lifespan)
        self.assertIn('_create_lifecycle_background_task(_digital_clock_guard_loop(), name="digital-clock-guard")',
                      source)


if __name__ == "__main__":
    unittest.main()
