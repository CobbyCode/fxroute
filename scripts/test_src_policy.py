#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Central SRC policy must not downgrade modes or create provider pipelines."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SrcPolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("audio.src_policy"), "Common SRC policy is missing")
        from audio import src_policy
        self.policy = src_policy
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        spa = self.root / "system-spa"
        spa.mkdir()
        locations = patch.object(self.policy, "plugin_locations", return_value=[spa / "libspa-fxroute-src.so"])
        locations.start()
        self.addCleanup(locations.stop)
        runtime = patch.object(self.policy, "RUNTIME", self.root / "native_src/build")
        runtime.start()
        self.addCleanup(runtime.stop)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_default_and_all_modes_roundtrip_independently_of_rate_policy(self):
        rate = self.root / "fxroute/sample-rate-policy.json"
        rate.parent.mkdir()
        rate.write_text('{"mode":"fixed","rate":48000}')
        self.assertEqual(self.policy.load_policy(), {"mode": "standard"})
        for mode in ("low", "standard", "high"):
            self.assertEqual(self.policy.persist_policy(mode), {"mode": mode})
            self.assertEqual(self.policy.load_policy(), {"mode": mode})
            self.assertEqual(json.loads(self.policy.policy_path().read_text()), {"mode": mode})
        self.assertEqual(json.loads(rate.read_text()), {"mode": "fixed", "rate": 48000})

    def test_invalid_policy_never_silently_selects_another_engine(self):
        for mode in ("", "VHQ", "HIGH", None, 2, "standard\n"):
            with self.assertRaises(ValueError):
                self.policy.persist_policy(mode)
        self.policy.policy_path().parent.mkdir(exist_ok=True)
        for payload in ('{"mode":"unknown"}', '[]', 'null', 'not json', '{"mode":"high","mode":"low"}', '{"mode":"high","extra":"value"}'):
            self.policy.policy_path().write_text(payload)
            with self.assertRaises(ValueError):
                self.policy.load_policy()

    def test_one_factory_is_bound_at_each_adapter_boundary(self):
        runtime = self.root / "application/native_src/build"
        runtime.mkdir(parents=True)
        (runtime / "libspa-fxroute-src.so").touch()
        (runtime / "sox").touch(mode=0o755)
        (runtime / "manifest.json").write_text('{"sox_version":"16.2.1"}')
        self.policy.install_plugin(runtime)
        paths = self.policy.configure(runtime)
        self.assertEqual(len(paths), 5)
        contents = {str(p.relative_to(self.root)): p.read_text() for p in paths}
        self.assertIn("pipewire/client.conf.d/80-fxroute-src.conf", contents)
        self.assertIn("pipewire/pipewire-pulse.conf.d/80-fxroute-src.conf", contents)
        self.assertIn("pipewire/pipewire.conf.d/80-fxroute-src.conf", contents)
        wp = contents["wireplumber/wireplumber.conf.d/80-fxroute-src.conf"]
        self.assertIn("monitor.alsa.rules", wp)
        self.assertIn("monitor.bluez.rules", wp)
        self.assertTrue(all('fxroute.src.convert' in text for name, text in contents.items() if not name.startswith('environment.d/')))
        self.assertTrue(all('libspa-fxroute-src' in text for name, text in contents.items() if not name.startswith('environment.d/')))
        self.assertNotIn("spotify", "".join(contents.values()))
        self.assertNotIn("qbzd", "".join(contents.values()))
        self.assertIn("environment.d/80-fxroute-src.conf", contents)

    def test_library_search_is_explicit_and_preserves_existing_plugin_directories(self):
        with patch.dict(os.environ, {"SPA_PLUGIN_DIR": "/vendor/spa"}):
            environment = self.policy.client_env({"LANG": "C"})
        self.assertEqual(environment["LANG"], "C")
        self.assertIn(str(self.policy.RUNTIME), environment["SPA_PLUGIN_DIR"].split(":"))
        self.assertIn("/vendor/spa", environment["SPA_PLUGIN_DIR"].split(":"))
        self.assertNotIn(str(self.policy.RUNTIME / "libspa-fxroute-src"),
                         self.policy.configuration()[self.root / "pipewire/client.conf.d/80-fxroute-src.conf"])

    def test_live_node_readback_uses_actual_flat_spa_param_pairs(self):
        from types import SimpleNamespace
        dump = [{"id": 42, "type": "PipeWire:Interface:Node", "info": {
            "state": "running", "props": {"node.name": "mpv", "audio.adapt.converter": "fxroute.src.convert"},
            "params": {"Props": [{"params": ["fxroute.src.mode", "high", "fxroute.src.input-rate", 44100,
                "fxroute.src.output-rate", 48000, "fxroute.src.nominal-bypass", False, "fxroute.src.error", 0]}]}}}]
        with patch.object(self.policy.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(dump))):
            nodes = self.policy.nodes()
        self.assertEqual(nodes[0]["mode"], "high")
        self.assertEqual(nodes[0]["input_rate"], 44100)
        self.assertFalse(nodes[0]["nominal_bypass"])

    def test_unbound_hardware_reports_a_binding_error(self):
        from types import SimpleNamespace
        dump = [{"id":42,"type":"PipeWire:Interface:Node","info":{"state":"suspended","props":{
            "node.name":"alsa_output.hw_test", "device.api":"alsa", "media.class":"Audio/Sink"}}}]
        with (patch.object(self.policy.subprocess,"run",return_value=SimpleNamespace(stdout=json.dumps(dump))),
              patch.object(self.policy, "runtime_error", return_value=None)):
            status = self.policy.status()
        self.assertFalse(status["available"])
        self.assertIn("alsa_output.hw_test", status["error"])

    def test_product_ingress_monitor_requires_the_fixed_converter_even_while_idle(self):
        from types import SimpleNamespace
        for state in ("running", "idle", "suspended"):
            for bound in (False, True):
                with self.subTest(state=state, bound=bound):
                    props = {"node.name": "fxroute_dsp_sink", "media.class": "Audio/Sink"}
                    params = {}
                    if bound:
                        props["audio.adapt.converter"] = self.policy.FACTORY
                        params = {"Props": [{"params": ["fxroute.src.mode", "standard", "fxroute.src.error", 0]}]}
                    dump = [{"id": 42, "type": "PipeWire:Interface:Node",
                             "info": {"state": state, "props": props, "params": params}}]
                    with (patch.object(self.policy.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(dump))),
                          patch.object(self.policy, "runtime_error", return_value=None)):
                        status = self.policy.status()
                    self.assertEqual(len(status["effective_nodes"]), 1)
                    self.assertTrue(status["effective_nodes"][0]["expected"])
                    self.assertEqual(status["available"], bound)
                    if not bound:
                        self.assertIn("fxroute_dsp_sink", status["error"])

    def test_no_observed_adapters_is_not_an_error(self):
        with (patch.object(self.policy, "runtime_error", return_value=None),
              patch.object(self.policy, "nodes", return_value=[])):
            status = self.policy.status()
        self.assertTrue(status["available"])
        self.assertIsNone(status["error"])
        self.assertEqual(status["effective_nodes"], [])

    def test_unwritable_policy_is_a_structured_runtime_failure(self):
        self.policy.persist_policy("high")
        with patch.object(self.policy, "atomic_write_text", side_effect=OSError(30, "Read-only file system")):
            with self.assertRaisesRegex(RuntimeError, "Cannot save SRC policy: .*Read-only file system"):
                self.policy.persist_policy("low")
        self.assertEqual(self.policy.load_policy(), {"mode": "high"})

    def test_running_unconfigured_or_unbound_stream_reports_a_binding_error(self):
        from types import SimpleNamespace
        for bound in (True, False):
            props = {"node.name":"mpv", "media.class":"Stream/Output/Audio"}
            if bound:
                props["audio.adapt.converter"] = self.policy.FACTORY
            dump = [{"id":42,"type":"PipeWire:Interface:Node", "info":{"state":"running","props":props,
                "params":{"Props":[{"params":["fxroute.src.mode","unconfigured"]}] if bound else []}}}]
            with (patch.object(self.policy.subprocess,"run",return_value=SimpleNamespace(stdout=json.dumps(dump))),
                  patch.object(self.policy,"runtime_error",return_value=None)):
                status = self.policy.status()
            self.assertFalse(status["available"])
            self.assertIn("mpv", status["error"])

    def test_inactive_unbound_stream_does_not_block_playback_src_selection(self):
        from types import SimpleNamespace
        self.policy.persist_policy("high")
        for state in ("idle", "suspended"):
            with self.subTest(state=state):
                dump = [
                    {"id": 48, "type": "PipeWire:Interface:Node", "info": {
                        "state": "running", "props": {
                            "node.name": "mpv", "media.class": "Stream/Output/Audio",
                            "audio.adapt.converter": self.policy.FACTORY},
                        "params": {"Props": [{"params": ["fxroute.src.mode", "high",
                            "fxroute.src.input-rate", 44100, "fxroute.src.output-rate", 44100,
                            "fxroute.src.nominal-bypass", True, "fxroute.src.error", 0]}]}}},
                    {"id": 185, "type": "PipeWire:Interface:Node", "info": {
                        "state": state, "props": {
                            "node.name": "spotify", "media.class": "Stream/Output/Audio"},
                        "params": {"Props": [{"params": ["resample.quality", 4]}]}}},
                ]
                with (patch.object(self.policy.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(dump))),
                      patch.object(self.policy, "runtime_error", return_value=None)):
                    status = self.policy.status()
                self.assertTrue(status["available"], status["error"])
                self.assertIsNone(status["error"])
                self.assertEqual(status["mode"], "high")
                inactive = next(node for node in status["effective_nodes"] if node["name"] == "spotify")
                self.assertFalse(inactive["bound"])
                self.assertFalse(inactive["expected"])
                self.assertIsNone(inactive["mode"])

    def test_inactive_bound_adapter_still_reports_mode_generation_and_engine_errors(self):
        runtime = self.policy.RUNTIME
        runtime.mkdir(parents=True)
        (runtime / "manifest.json").write_text('{"generation":"current"}')
        for state in ("idle", "suspended"):
            for mode, generation, error in (("high", "current", 0),
                                             ("standard", "old", 0),
                                             ("standard", "current", 32)):
                with self.subTest(state=state, mode=mode, generation=generation, error=error):
                    node = {"name": "mpv", "state": state, "bound": True, "expected": False,
                            "mode": mode, "generation": generation, "error": error}
                    with (patch.object(self.policy, "nodes", return_value=[node]),
                          patch.object(self.policy, "runtime_error", return_value=None)):
                        status = self.policy.status()
                    self.assertFalse(status["available"])
                    self.assertIsNotNone(status["error"])

    def test_unbound_stream_with_unknown_or_failed_state_still_requires_binding(self):
        from types import SimpleNamespace
        for state in (None, "creating", "error", "unknown"):
            with self.subTest(state=state):
                dump = [{"id": 42, "type": "PipeWire:Interface:Node", "info": {
                    "state": state, "props": {
                        "node.name": "foreign-stream", "media.class": "Stream/Output/Audio"}}}]
                with (patch.object(self.policy.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(dump))),
                      patch.object(self.policy, "runtime_error", return_value=None)):
                    status = self.policy.status()
                self.assertTrue(status["effective_nodes"][0]["expected"])
                self.assertFalse(status["available"])

    def test_live_graph_check_allows_inactive_diagnostics_but_rejects_wrong_bound_mode(self):
        import test_src_product_live as live
        src = {"mode": "standard", "available": True, "effective_nodes": [
            {"name": "mpv", "state": "running", "bound": True, "expected": True,
             "mode": "standard", "error": 0},
            {"name": "spotify", "state": "idle", "bound": False, "expected": False,
             "mode": None, "error": 0},
        ]}
        outputs = {"selected_output": {"target_name": "alsa_output.test"}}
        with (patch.object(live, "http", side_effect=lambda path: src if path == "/api/audio/src" else outputs),
              patch.object(live.subprocess, "check_output",
                           return_value="fxroute_dsp:output_FL -> alsa_output.test:playback_FL")):
            observed, sink = live.graph("standard")
            self.assertEqual(sink, "alsa_output.test")
            self.assertEqual(len(observed["effective_nodes"]), 2)
            src["effective_nodes"][0]["mode"] = "high"
            with self.assertRaises(AssertionError):
                live.graph("standard")

    def test_plugin_is_discoverable_without_client_environment_injection(self):
        runtime = self.root / "native_src/build"
        runtime.mkdir(parents=True)
        library = runtime / "libspa-fxroute-src.so"
        library.write_bytes(b"library")
        installed = self.policy.install_plugin(runtime)
        self.assertEqual(installed.resolve(), library.resolve())
        self.assertEqual(installed.read_bytes(), b"library")

    def test_missing_privileged_plugin_does_not_publish_client_bindings(self):
        runtime=self.policy.RUNTIME
        runtime.mkdir(parents=True)
        (runtime / "libspa-fxroute-src.so").touch()
        (runtime / "sox").touch(mode=0o755)
        existing=self.root / "pipewire/client.conf.d/80-fxroute-src.conf"
        existing.parent.mkdir(parents=True)
        existing.write_text("# Existing client configuration\n")
        with self.assertRaisesRegex(RuntimeError,"standard SPA path"):
            self.policy.configure()
        self.assertEqual(existing.read_text(),"# Existing client configuration\n")
        self.assertFalse((self.root / "wireplumber/wireplumber.conf.d/80-fxroute-src.conf").exists())

    def test_graph_only_split_loopbacks_are_not_native_adapter_boundaries(self):
        from types import SimpleNamespace
        dump = [
            {"id":42,"type":"PipeWire:Interface:Node","info":{"state":"running","props":{
                "node.name":"alsa_output.hw_test", "device.api":"alsa", "media.class":"Audio/Sink", "audio.adapt.converter":self.policy.FACTORY},
                "params":{"Props":[{"params":["fxroute.src.mode","standard"]}]}}},
            {"id":43,"type":"PipeWire:Interface:Node","info":{"state":"suspended","props":{
                "node.name":"alsa_output.test.split", "node.virtual":True,"node.passive":True,
                "node.link-group":"loopback-123", "media.class":"Stream/Output/Audio"}}},
            {"id":44,"type":"PipeWire:Interface:Node","info":{"state":"suspended","props":{
                "node.name":"Midi-Bridge", "device.api":"alsa", "media.class":"Midi/Bridge"}}},
            {"id":45,"type":"PipeWire:Interface:Node","info":{"state":"suspended","props":{
                "node.name":"bluez_midi.server", "device.api":"bluez5", "media.class":"Midi/Bridge"}}},
        ]
        with (patch.object(self.policy.subprocess,"run",return_value=SimpleNamespace(stdout=json.dumps(dump))),
              patch.object(self.policy,"runtime_error",return_value=None)):
            status=self.policy.status()
        self.assertTrue(status["available"])
        self.assertEqual(len(status["effective_nodes"]),1)

    def test_playback_error_flags_a_bound_runtime_that_new_streams_cannot_load(self):
        binding = self.root / "pipewire/client.conf.d/80-fxroute-src.conf"
        runtime = self.root / "native_src/build"
        # Without the client binding a missing runtime cannot break playback.
        self.assertIsNone(self.policy.playback_error())
        binding.parent.mkdir(parents=True)
        binding.write_text("stream.rules = []\n")
        self.assertRegex(self.policy.playback_error(), "runtime is missing")
        # Deploy accident: the stable links point at an empty generation.
        runtime.mkdir(parents=True)
        (runtime / "runtime-empty").mkdir()
        (runtime / "current").symlink_to("runtime-empty")
        (runtime / "libspa-fxroute-src.so").symlink_to("current/libspa-fxroute-src.so")
        self.assertRegex(self.policy.playback_error(), "runtime is missing")
        # Repointed at a complete generation.
        (runtime / "runtime-built").mkdir()
        (runtime / "runtime-built/libspa-fxroute-src.so").write_bytes(b"plugin")
        (runtime / "current").unlink()
        (runtime / "current").symlink_to("runtime-built")
        self.assertIsNone(self.policy.playback_error())


if __name__ == "__main__":
    unittest.main()
