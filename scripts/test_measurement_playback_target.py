#!/usr/bin/env python3
"""ACTIVE_CHAIN measurement playback-target resolution contracts.

ACTIVE_CHAIN must resolve to the active EasyEffects chain (easyeffects_sink)
in every output mode and fail closed when that chain is unavailable.  The
explicit raw/helper scope keeps resolving to the hardware sink.
"""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from measurement import (
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
    MeasurementStore,
)


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

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, overview, scope=MEASUREMENT_SCOPE_ACTIVE_CHAIN, ports_present=True):
        with patch("measurement.get_audio_output_overview", return_value=overview), patch.object(
            self.store, "_list_pw_ports", return_value=(
                ["easyeffects_sink:playback_FL", "easyeffects_sink:playback_FR"]
                if ports_present else []
            )
        ):
            return self.store._resolve_playback_target(measurement_scope=scope)

    def test_active_chain_resolves_to_easyeffects_sink_in_stereo(self):
        target = self._resolve(stereo_overview())
        self.assertEqual(target["target_name"], "easyeffects_sink")

    def test_active_chain_resolves_to_easyeffects_sink_in_subwoofer(self):
        target = self._resolve(subwoofer_overview())
        self.assertEqual(target["target_name"], "easyeffects_sink")

    def test_active_chain_fails_closed_when_easyeffects_sink_ports_missing(self):
        with self.assertRaises(RuntimeError) as caught:
            self._resolve(stereo_overview(), ports_present=False)
        self.assertIn("easyeffects_sink", str(caught.exception))
        self.assertNotIn("alsa_output.hw", str(caught.exception))

    def test_raw_helper_scope_keeps_hardware_sink(self):
        target = self._resolve(stereo_overview(), scope=MEASUREMENT_SCOPE_RAW_HELPER)
        self.assertEqual(target["target_name"], "alsa_output.hw")

    def test_subwoofer_active_chain_repairs_missing_easyeffects_helper_links(self):
        playback_target = {"target_name": "easyeffects_sink"}
        playback_route = {
            "route": "subwoofer-active-chain",
            "output_mode": "subwoofer-2.2",
            "playback_target_name": "easyeffects_sink",
            "helper_node_name": "fxroute_21_stage1",
        }
        existing_links = set()
        creation_order = []

        def create_link(source_port, target_port):
            creation_order.append((source_port, target_port))
            existing_links.add((source_port, target_port))

        with patch.object(
            self.store,
            "_wait_for_measurement_play_ports",
            return_value={"left": "measure:output_FL", "right": "measure:output_FR"},
        ), patch.object(
            self.store,
            "_list_pw_ports",
            return_value=["easyeffects_sink:playback_FL", "easyeffects_sink:playback_FR"],
        ), patch.object(
            self.store, "_create_pipewire_link", side_effect=create_link
        ) as create, patch.object(
            self.store,
            "_pipewire_link_exists",
            side_effect=lambda source, target: (source, target) in existing_links,
        ), patch.object(
            self.store, "_remove_subwoofer_direct_easyeffects_hardware_links", return_value=[]
        ), patch.object(
            self.store, "_find_subwoofer_direct_easyeffects_hardware_links", return_value=[]
        ), patch.object(
            self.store, "_list_relevant_pw_links", return_value=[]
        ):
            diagnostics = self.store._link_measurement_playback_to_active_chain(
                play_node_name="measure",
                playback_target=playback_target,
                playback_route=playback_route,
            )

        created_links = [call.args for call in create.call_args_list]
        self.assertIn(("ee_soe_output_level:output_FL", "fxroute_21_stage1:input_L"), created_links)
        self.assertIn(("ee_soe_output_level:output_FR", "fxroute_21_stage1:input_R"), created_links)
        self.assertNotIn(("measure:output_FL", "alsa_output.hw:playback_FL"), created_links)
        self.assertLess(
            creation_order.index(("ee_soe_output_level:output_FR", "fxroute_21_stage1:input_R")),
            creation_order.index(("measure:output_FL", "easyeffects_sink:playback_FL")),
        )
        self.assertEqual(len(diagnostics["active_chain_output_links"]), 2)

    def test_measurement_cleanup_does_not_remove_active_chain_output_links(self):
        temporary_links = [
            {
                "source_port": "measure:output_FL",
                "target_port": "easyeffects_sink:playback_FL",
            },
            {
                "source_port": "measure:output_FR",
                "target_port": "easyeffects_sink:playback_FR",
            },
        ]
        with patch.object(self.store, "_disconnect_link", return_value=True) as disconnect, patch(
            "measurement.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            self.store._cleanup_measurement_playback_links(
                play_node_name="measure",
                temporary_links=temporary_links,
            )

        removed_links = [call.args for call in disconnect.call_args_list]
        self.assertEqual(
            removed_links,
            [
                ("measure:output_FL", "easyeffects_sink:playback_FL"),
                ("measure:output_FR", "easyeffects_sink:playback_FR"),
            ],
        )
        self.assertNotIn(
            ("ee_soe_output_level:output_FL", "fxroute_21_stage1:input_L"),
            removed_links,
        )


if __name__ == "__main__":
    unittest.main()
