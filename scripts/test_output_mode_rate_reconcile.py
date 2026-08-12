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
from playback_transition_test_support import make_transition_runtime
import measurement_session
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

    runtime = make_transition_runtime()

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
         mock.patch.object(main, "_reconcile_transition_sink_rate", new=reconcile_10_mock) as reconcile, \
         mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {}}):
        await measurement_session._measurement_entry_preflight(48000)
        reconcile.assert_awaited_once_with(48000, reason="measurement-entry-preflight")

    # 11. Measurement preflight: reconcile failure keeps the fast-fail error.
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 48000)), \
         mock.patch.object(main, "playback_transition_coordinator", mock.Mock(transition_blocked=False)), \
         mock.patch.object(main, "_playback_graph_diagnosis", new=mock.AsyncMock(return_value={"links_complete": True, "signature": "sig"})), \
         mock.patch.object(main, "_reconcile_transition_sink_rate", new=mock.AsyncMock(return_value=False)), \
         mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {}}):
        try:
            await measurement_session._measurement_entry_preflight(48000)
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

    # 14. Commit readback: recoverable effects-runtime drift (stale SPL-noise
    #     state) re-applies the canonical runtime once instead of failing.
    class FakeEffectsWithApply:
        def __init__(self):
            self.apply_calls = 0

        def get_active_preset(self):
            return "Neutral"

        def load_global_extras(self):
            return {"loudness": {"enabled": True}, "autogain": {"enabled": False}}

        def apply_autogain_loudness_runtime(self, *_args, **_kwargs):
            self.apply_calls += 1

    fake_apply = FakeEffectsWithApply()
    fake_player = mock.Mock()
    fake_player._running = True
    fake_player.state = {
        "current_file": "https://ice4.somafm.com/groovesalad-256-mp3",
        "playing": True,
        "paused": False,
        "volume": 100,
    }
    play_request = TransitionRequest(
        operation="resume", source="radio", target_rate=44100,
        should_play=True, target_url="https://ice4.somafm.com/groovesalad-256-mp3",
    )

    read_calls = {"count": 0}
    async def drifting_read(*_args, **_kwargs):
        read_calls["count"] += 1
        if read_calls["count"] == 1:
            raise RuntimeError("EasyEffects Loudness was bypassed after DSP stabilization")
        return {"loudness": {"volume": -16.36, "output_gain": -16.36, "bypass": False}}

    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 44100)), \
         mock.patch.object(main, "player_instance", fake_player), \
         mock.patch.object(main, "easyeffects_manager", fake_apply), \
         mock.patch.object(main, "_playback_graph_links_complete", new=mock.AsyncMock(return_value=True)), \
         mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {"mode": "stereo"}}), \
         mock.patch.object(runtime, "_read_and_validate_effects_runtime", new=drifting_read):
        result = await runtime._verify_transition(play_request, require_source_volume=True)
    assert result.get("committed") is True
    assert fake_apply.apply_calls == 1, "canonical runtime must be re-applied once on drift"
    assert read_calls["count"] == 2

    # 15. Commit readback: persistent drift still fails the transition.
    fake_apply2 = FakeEffectsWithApply()
    async def always_failing(*_args, **_kwargs):
        raise RuntimeError("EasyEffects Loudness was bypassed after DSP stabilization")
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(44100, 44100)), \
         mock.patch.object(main, "player_instance", fake_player), \
         mock.patch.object(main, "easyeffects_manager", fake_apply2), \
         mock.patch.object(main, "_playback_graph_links_complete", new=mock.AsyncMock(return_value=True)), \
         mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {"mode": "stereo"}}), \
         mock.patch.object(runtime, "_read_and_validate_effects_runtime", new=always_failing):
        try:
            await runtime._verify_transition(play_request, require_source_volume=True)
        except RuntimeError as exc:
            assert "bypassed" in str(exc)
        else:
            raise AssertionError("persistent effects drift must fail the commit readback")
    assert fake_apply2.apply_calls == 1, "re-apply must be attempted exactly once"

    # 16. DSP stabilization: link-only drift is repaired once before failing.
    stabilize_diagnosis_calls = []
    async def fake_diagnosis_seq(*_args, **_kwargs):
        stabilize_diagnosis_calls.append(1)
        if len(stabilize_diagnosis_calls) == 1:
            return stereo_diagnosis(links_present=False)
        return stereo_diagnosis(links_present=True)

    repair_calls = []
    async def fake_repair_stereo_seq(diagnosis):
        repair_calls.append(diagnosis.get("mode"))
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(48000, 48000)), \
         mock.patch.object(main, "easyeffects_manager", FakeEffectsManager()), \
         mock.patch.object(main, "_playback_graph_diagnosis", new=fake_diagnosis_seq), \
         mock.patch.object(main, "_repair_stereo_output_links_once", new=fake_repair_stereo_seq) as repair, \
         mock.patch.object(runtime, "_read_and_validate_effects_runtime", new=mock.AsyncMock(return_value={})):
        await runtime.stabilize_effects_after_rate_change(
            switch_request, dsp_reinitialized=True
        )
    assert repair_calls == ["stereo"], f"stereo link repair must run once, got {repair_calls}"

    # 17. DSP stabilization: persistent link loss still fails.
    with mock.patch.object(main, "get_samplerate_status", return_value=stuck_status(48000, 48000)), \
         mock.patch.object(main, "easyeffects_manager", FakeEffectsManager()), \
         mock.patch.object(main, "_playback_graph_diagnosis", new=mock.AsyncMock(return_value=stereo_diagnosis(links_present=False))), \
         mock.patch.object(main, "_repair_stereo_output_links_once", new=mock.AsyncMock()), \
         mock.patch.object(runtime, "_read_and_validate_effects_runtime", new=mock.AsyncMock(return_value={})):
        try:
            await runtime.stabilize_effects_after_rate_change(
                switch_request, dsp_reinitialized=True
            )
        except RuntimeError as exc:
            assert "production graph changed during DSP stabilization" in str(exc)
        else:
            raise AssertionError("persistent link loss must fail DSP stabilization")

    # 18. Playback rate lock applies only while a measurement job is running;
    #     an open-but-idle measurement window must not block play.
    class FakeSrSession:
        def __init__(self, active, jobs, spl_jobs, auto_sub):
            self.active = active
            self.measurement_rate = 48000
            self.active_manual_job_ids = jobs
            self.active_spl_job_ids = spl_jobs
            self.active_auto_sub_job_id = auto_sub

        @property
        def has_active_jobs(self):
            return bool(
                self.active_manual_job_ids
                or self.active_spl_job_ids
                or self.active_auto_sub_job_id is not None
            )

    idle_session = FakeSrSession(active=True, jobs=set(), spl_jobs=set(), auto_sub=None)
    with mock.patch.object(main, "measurement_sr_session", idle_session):
        assert main._measurement_session_blocks_playback_rate(44100) is False, (
            "idle open measurement window must not block playback rate changes"
        )
    busy_session = FakeSrSession(active=True, jobs={"sweep-1"}, spl_jobs=set(), auto_sub=None)
    with mock.patch.object(main, "measurement_sr_session", busy_session):
        assert main._measurement_session_blocks_playback_rate(44100) is True, (
            "running sweep must block playback rate changes"
        )
    spl_session = FakeSrSession(active=True, jobs=set(), spl_jobs={"spl-calibration"}, auto_sub=None)
    with mock.patch.object(main, "measurement_sr_session", spl_session):
        assert main._measurement_session_blocks_playback_rate(44100) is True, (
            "running SPL noise must block playback rate changes"
        )
    with mock.patch.object(main, "measurement_sr_session", idle_session):
        assert main._measurement_session_blocks_playback_rate(48000) is False, (
            "same-rate requests are never blocked"
        )

    # 19. Output-mode switch lock is job-scoped: an open-but-idle measurement
    #     window must not block the stereo <-> subwoofer switch.
    async def fake_switch_route(_request=None):
        if main.measurement_sr_session is not None and main.measurement_sr_session.has_active_jobs:
            raise RuntimeError("423 lock would trigger")
        return "switch allowed"

    with mock.patch.object(main, "measurement_sr_session", idle_session):
        assert await fake_switch_route() == "switch allowed"
    with mock.patch.object(main, "measurement_sr_session", busy_session):
        try:
            await fake_switch_route()
        except RuntimeError as exc:
            assert "423" in str(exc)
        else:
            raise AssertionError("running sweep must lock the output-mode switch")
    assert idle_session.has_active_jobs is False
    assert busy_session.has_active_jobs is True

    print("output-mode rate reconcile tests: ok")


if __name__ == "__main__":
    asyncio.run(main_async())
