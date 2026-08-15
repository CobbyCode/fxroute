#!/usr/bin/env python3
"""ACTIVE_CHAIN measurement playback-target resolution contracts.

ACTIVE_CHAIN must resolve to the active DSP chain (fxroute_dsp_sink)
in every output mode and fail closed when that chain is unavailable.  The
explicit raw/helper scope keeps resolving to the hardware sink.
"""

import pathlib
import asyncio
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from measurement.store import (
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
    MeasurementStore,
)
from measurement.routing import MeasurementRouting


def stereo_overview():
    return {
        "output_mode": {"mode": "stereo"},
        "current_output": {"name": "alsa_output.hw"},
        "selected_output": {"target_name": "alsa_output.hw"},
        "default_output": {"target_name": "alsa_output.hw"},
    }


def subwoofer_overview():
    return {
        "output_mode": {"mode": "subwoofer-2.2"},
        "current_output": {"name": "alsa_output.hw"},
        "selected_output": {"target_name": "alsa_output.hw"},
        "default_output": {"target_name": "alsa_output.hw"},
    }


class MeasurementPlaybackTargetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MeasurementStore(home=pathlib.Path(self._tmp.name))
        self.assertIsInstance(self.store._routing, MeasurementRouting)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, overview, scope=MEASUREMENT_SCOPE_ACTIVE_CHAIN, ports_present=True):
        with patch("measurement.store.get_audio_output_overview", return_value=overview), patch.object(
            self.store, "_list_pw_ports", return_value=(
                ["fxroute_dsp_sink:playback_FL", "fxroute_dsp_sink:playback_FR"]
                if ports_present else []
            )
        ):
            return self.store._resolve_playback_target(measurement_scope=scope)

    def test_active_chain_resolves_to_fxroute_dsp_sink_in_stereo(self):
        target = self._resolve(stereo_overview())
        self.assertEqual(target["target_name"], "fxroute_dsp_sink")

    def test_active_chain_resolves_to_fxroute_dsp_sink_in_subwoofer(self):
        target = self._resolve(subwoofer_overview())
        self.assertEqual(target["target_name"], "fxroute_dsp_sink")

    def test_active_chain_fails_closed_when_fxroute_dsp_sink_ports_missing(self):
        with self.assertRaises(RuntimeError) as caught:
            self._resolve(stereo_overview(), ports_present=False)
        self.assertIn("fxroute_dsp_sink", str(caught.exception))
        self.assertNotIn("alsa_output.hw", str(caught.exception))

    def test_raw_helper_scope_uses_native_ingress_sink(self):
        target = self._resolve(stereo_overview(), scope=MEASUREMENT_SCOPE_RAW_HELPER)
        self.assertEqual(target["target_name"], "fxroute_dsp_sink")

    def test_both_scopes_use_real_native_ingress_topology(self):
        target = {"target_name": "fxroute_dsp_sink"}
        for scope in (MEASUREMENT_SCOPE_ACTIVE_CHAIN, MEASUREMENT_SCOPE_RAW_HELPER):
            route = self.store._build_measurement_playback_route(
                "measure", target, measurement_scope=scope, overview=subwoofer_overview())
            self.assertEqual(route["route"], "direct-sink")
            self.assertEqual(route["playback_target_name"], "fxroute_dsp_sink")

    def test_pre_sweep_validation_uses_native_runtime_config_not_legacy_argv(self):
        self.store.runtime_snapshot_provider = lambda: {
            "active": True,
            "config": {"sample_rate": 48000, "output_mode": "subwoofer-2.2", "layout": [{}, {}, {}, {}]},
            "effect_bypass": True,
        }
        snapshot = self.store._routing._build_pre_sweep_state_snapshot(
            job_id="job", sample_rate=48000,
            playback_route={"route": "direct-sink", "measurement_scope": MEASUREMENT_SCOPE_RAW_HELPER,
                            "output_mode": "subwoofer-2.2"})
        self.assertIsNone(snapshot["validation_failure"])
        self.assertNotIn("parsed", snapshot)

    def test_raw_helper_bypass_is_restored_after_worker_failure(self):
        calls = []

        async def set_bypass(value):
            calls.append(value)
            return False

        self.store.effect_bypass_setter = set_bypass
        self.store._jobs["job"] = {
            "id": "job", "status": "queued", "measurement_scope": MEASUREMENT_SCOPE_RAW_HELPER,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.store._execute_capture_job = lambda _job: (_ for _ in ()).throw(RuntimeError("capture failed"))
        asyncio.run(self.store._run_measurement_job("job"))
        self.assertEqual(calls, [True, False])
        self.assertEqual(self.store._jobs["job"]["status"], "failed")

    def test_active_chain_does_not_change_effect_bypass(self):
        calls = []

        async def set_bypass(value):
            calls.append(value)
            return False

        self.store.effect_bypass_setter = set_bypass
        self.store._jobs["job"] = {
            "id": "job", "status": "queued", "measurement_scope": MEASUREMENT_SCOPE_ACTIVE_CHAIN,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.store._execute_capture_job = lambda _job: {"message": "ok"}
        asyncio.run(self.store._run_measurement_job("job"))
        self.assertEqual(calls, [])
        self.assertEqual(self.store._jobs["job"]["status"], "completed")

    def test_measurement_cleanup_does_not_remove_active_chain_output_links(self):
        temporary_links = [
            {
                "source_port": "measure:output_FL",
                "target_port": "fxroute_dsp_sink:playback_FL",
            },
            {
                "source_port": "measure:output_FR",
                "target_port": "fxroute_dsp_sink:playback_FR",
            },
        ]
        with patch.object(self.store, "_disconnect_link", return_value=True) as disconnect, patch(
            "measurement.store.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            self.store._routing._cleanup_measurement_playback_links(
                play_node_name="measure",
                temporary_links=temporary_links,
            )

        removed_links = [call.args for call in disconnect.call_args_list]
        self.assertEqual(
            removed_links,
            [
                ("measure:output_FL", "fxroute_dsp_sink:playback_FL"),
                ("measure:output_FR", "fxroute_dsp_sink:playback_FR"),
            ],
        )
        self.assertNotIn(("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"), removed_links)


if __name__ == "__main__":
    unittest.main()
