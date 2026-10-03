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

    def test_disk_readiness_does_not_claim_effective_activation(self):
        with (patch.object(self.policy, "runtime_error", return_value=None),
              patch.object(self.policy, "nodes", return_value=[])):
            status = self.policy.status()
        self.assertFalse(status["available"])
        self.assertTrue(status["restart_required"])

    def test_running_unconfigured_or_unbound_stream_requires_activation(self):
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
            self.assertTrue(status["restart_required"])

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


if __name__ == "__main__":
    unittest.main()
