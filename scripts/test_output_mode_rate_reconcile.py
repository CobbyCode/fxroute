#!/usr/bin/env python3
"""Regression test: output-mode transition sink-rate reconcile.

Covers the found failure mode (2.2 <-> stereo output-mode regression):

- The effects/helper graph rebuild inside a transition can leave the hardware
  sink suspended at the configured default rate (44100) while
  clock.force-rate=48000 is set.
- A suspended sink ignores force-rate writes and suspend/resume pulses; the
  only proven renegotiation trigger is a short silent stream.
- The transition rate stages must reconcile (force -> silent trigger) before
  committing instead of hard-failing on the first mismatched readback.
"""

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import TransitionRequest


def stuck_status(active_rate: int, force_rate: int) -> dict:
    """The known failure state: sink at 44100 while force-rate=48000."""
    return {
        "status": "ok",
        "active_rate": active_rate,
        "force_rate": force_rate,
        "clock_rate": active_rate,
        "mode": "fixed" if force_rate else "auto",
    }


async def main_async() -> None:
    # 1. Trigger file naming
    assert main._rate_renegotiation_trigger_path(48000).name == (
        "fxroute-rate-renegotiation-trigger-48000.wav"
    )

    # 2. Reconcile: already aligned -> no force write, no trigger
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(48000, 48000)) as status, \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=True)) as force, \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=mock.AsyncMock(return_value=True)) as trigger:
        assert await main._reconcile_transition_sink_rate(48000, reason="test") is True
        status.assert_called_once()
        force.assert_not_awaited()
        trigger.assert_not_awaited()

    # 3. Reconcile: stuck sink, force fails, silent trigger aligns -> True
    status_3 = stuck_status(44100, 48000)
    async def trigger_3(_rate):
        status_3.update(stuck_status(48000, 48000))
        return True
    trigger_3_mock = mock.AsyncMock(side_effect=trigger_3)
    with mock.patch.object(main, "get_samplerate_status", return_value=status_3), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=False)) as force, \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=trigger_3_mock) as trigger:
        assert await main._reconcile_transition_sink_rate(48000, reason="test") is True
        force.assert_awaited_once()
        trigger.assert_awaited_once_with(48000)

    # 4. Reconcile: force alone aligns -> trigger must not run
    status_4 = stuck_status(44100, 48000)
    async def force_4(_rate, *_args, **_kwargs):
        status_4.update(stuck_status(48000, 48000))
        return True
    force_4_mock = mock.AsyncMock(side_effect=force_4)
    with mock.patch.object(main, "get_samplerate_status", return_value=status_4), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=force_4_mock) as force, \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=mock.AsyncMock(return_value=True)) as trigger:
        assert await main._reconcile_transition_sink_rate(48000, reason="test") is True
        force.assert_awaited_once()
        trigger.assert_not_awaited()

    # 5. Reconcile: stuck sink, force AND trigger fail -> False (verifier still raises)
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 48000)), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=False)), \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=mock.AsyncMock(return_value=False)):
        assert await main._reconcile_transition_sink_rate(48000, reason="test") is False

    runtime = main.FxrouteTransitionRuntime()

    # 6. establish_target_rate: stuck sink, trigger aligns -> no raise
    request = TransitionRequest(operation="measurement-entry", source="local", target_rate=48000)
    status_6 = stuck_status(44100, 48000)
    async def trigger_6(_rate):
        status_6.update(stuck_status(48000, 48000))
        return True
    trigger_6_mock = mock.AsyncMock(side_effect=trigger_6)
    with mock.patch.object(main, "get_samplerate_status", return_value=status_6), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=False)), \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=trigger_6_mock) as trigger:
        await runtime.establish_target_rate(request)
        trigger.assert_awaited_once_with(48000)

    # 7. establish_target_rate: aligned -> force/trigger untouched
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(48000, 48000)), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=True)) as force, \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=mock.AsyncMock(return_value=True)) as trigger:
        await runtime.establish_target_rate(request)
        force.assert_not_awaited()
        trigger.assert_not_awaited()

    # 8. establish_target_rate: still stuck after trigger -> original error
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 48000)), \
         mock.patch.object(main, "_ensure_playback_samplerate_force", new=mock.AsyncMock(return_value=False)), \
         mock.patch.object(main, "_trigger_idle_sink_renegotiation", new=mock.AsyncMock(return_value=False)):
        try:
            await runtime.establish_target_rate(request)
        except RuntimeError as exc:
            assert "did not settle" in str(exc)
        else:
            raise AssertionError("establish_target_rate must raise when the rate never settles")

    # 9. DSP stabilization: output-mode switch without playback must not gate
    #    the graph readback on source links (mpv has no ports when idle).
    class FakeEffectsManager:
        LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0.0

        def load_global_extras(self):
            return {"loudness": {"enabled": True}, "autogain": {"enabled": False}}

        def apply_autogain_loudness_runtime(self, *_args, **_kwargs):
            return {}

    for should_play, expected_source, expected_require in (
        (False, None, False),
        (True, "local", True),
    ):
        diagnose_calls = []
        async def fake_diagnosis(*_args, **_kwargs):
            diagnose_calls.append((_kwargs.get("source"), _kwargs.get("require_source")))
            return {"links_complete": True, "signature": "sig"}
        switch_request = TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            should_play=should_play, target_url=None,
        )
        with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(48000, 48000)), \
             mock.patch.object(main, "easyeffects_manager", FakeEffectsManager()), \
             mock.patch.object(main, "_playback_graph_diagnosis", new=fake_diagnosis), \
             mock.patch.object(runtime, "_read_and_validate_effects_runtime", new=mock.AsyncMock(return_value={})):
            await runtime.stabilize_effects_after_rate_change(
                switch_request, dsp_reinitialized=True
            )
        assert diagnose_calls == [(expected_source, expected_require)], (
            f"stabilize diagnosis must use source={expected_source} require_source={expected_require}, "
            f"got {diagnose_calls}"
        )

    # 10. Measurement preflight: stuck sink reconciles via trigger instead of
    #     hard-failing (sweep start with an idle 44.1 kHz sink).
    status_10 = stuck_status(44100, 48000)
    async def reconcile_10(_rate, **_kwargs):
        status_10.update(stuck_status(48000, 48000))
        return True
    reconcile_10_mock = mock.AsyncMock(side_effect=reconcile_10)
    with mock.patch.object(main, "get_samplerate_status", return_value=status_10), \
         mock.patch.object(main, "playback_transition_coordinator", mock.Mock(transition_blocked=False)), \
         mock.patch.object(main, "_playback_graph_diagnosis", new=mock.AsyncMock(return_value={"links_complete": True, "signature": "sig"})), \
         mock.patch.object(main, "_reconcile_transition_sink_rate", new=reconcile_10_mock) as reconcile:
        await main._measurement_entry_preflight(48000)
        reconcile.assert_awaited_once_with(48000, reason="measurement-entry-preflight")

    # 11. Measurement preflight: reconcile failure keeps the fast-fail error.
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 48000)), \
         mock.patch.object(main, "playback_transition_coordinator", mock.Mock(transition_blocked=False)), \
         mock.patch.object(main, "_playback_graph_diagnosis", new=mock.AsyncMock(return_value={"links_complete": True, "signature": "sig"})), \
         mock.patch.object(main, "_reconcile_transition_sink_rate", new=mock.AsyncMock(return_value=False)):
        try:
            await main._measurement_entry_preflight(48000)
        except RuntimeError as exc:
            assert "preflight rate mismatch" in str(exc)
        else:
            raise AssertionError("measurement preflight must fail when the rate never settles")

    # 12. Measurement-session link-loss reconcile: stereo EE->hardware link
    #     drift is repairable; missing EE ports / rate mismatch are not.
    def stereo_diagnosis(links_present, *, ee_ports=True, aligned=True, mode="stereo"):
        links = {
            "ee_soe_output_level:output_FL -> alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_FL": links_present,
            "ee_soe_output_level:output_FR -> alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_FR": links_present,
        }
        return {
            "links_complete": ee_ports and links_present,
            "mode": mode,
            "output_key": "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40",
            "ee_ports": ee_ports,
            "helper_ports": None,
            "helper_active": None,
            "helper_rate": None,
            "helper_rate_matches": None,
            "measurement_rate_aligned": aligned,
            "direct_ee_to_hw_present": False,
            "links": links,
        }

    assert main._measurement_session_link_loss_is_repairable(
        stereo_diagnosis(links_present=False), target_rate=48000
    ) is True, "stereo EE->hardware link drift must be repairable"
    assert main._measurement_session_link_loss_is_repairable(
        stereo_diagnosis(links_present=True), target_rate=48000
    ) is False, "complete graph is not a link loss"
    assert main._measurement_session_link_loss_is_repairable(
        stereo_diagnosis(links_present=False, ee_ports=False), target_rate=48000
    ) is False, "missing EE ports are not a link-only loss"
    assert main._measurement_session_link_loss_is_repairable(
        stereo_diagnosis(links_present=False, aligned=False), target_rate=48000
    ) is False, "rate mismatch is not a link-only loss"

    # 13. Stereo reconciler must pick the stereo link repair path.
    reconciler_calls = []
    async def fake_repair_stereo(diagnosis):
        reconciler_calls.append(diagnosis.get("mode"))
    repair_stereo_mock = mock.AsyncMock(side_effect=fake_repair_stereo)
    with mock.patch.object(main, "_playback_graph_diagnosis", new=mock.AsyncMock(return_value=stereo_diagnosis(False))), \
         mock.patch.object(main, "_repair_stereo_output_links_once", new=repair_stereo_mock) as repair, \
         mock.patch.object(main, "_coordinator_reconcile_subwoofer_links_only", new=mock.AsyncMock()) as sub_repair:
        await runtime.reconcile_measurement_session_graph(48000)
        repair.assert_awaited_once()
        sub_repair.assert_not_awaited()
        assert reconciler_calls == ["stereo"]

    print("output-mode rate reconcile tests: ok")


if __name__ == "__main__":
    asyncio.run(main_async())
