#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Record links must be verified streaming before the sweep plays.

Regression: link creation was fire-and-forget (a blind 0.15s sleep), so a
link that never materialized produced a silent channel and a confusing
analysis failure minutes later. Verification polls `pw-link -l` until both
expected pairs appear (or fails closed), then settles briefly.
"""

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


def link_line(src, dst, link_id=42):
    """Legacy single-line listing shape (older PipeWire)."""
    return f"{src} -> {dst} (id: {link_id})"


def tree_block(src, dst):
    """Current tree listing shape (ground truth from .104)."""
    return f"{src}\n  |-> {dst}"


class PairParserTests(unittest.TestCase):
    def test_tree_pairs(self):
        listing = "\n".join([
            "fxroute_dsp_sink:monitor_FR",
            "  |-> fxroute-measure-record-jo:input_FL",
            "alsa_output:playback_AUX0",
            "  |<- fxroute_dsp:output_1",
        ])
        from measurement.routing import iter_pw_link_pairs
        self.assertEqual(iter_pw_link_pairs(listing), [
            ("fxroute_dsp_sink:monitor_FR", "fxroute-measure-record-jo:input_FL"),
            ("fxroute_dsp:output_1", "alsa_output:playback_AUX0"),
        ])

    def test_legacy_pairs(self):
        from measurement.routing import iter_pw_link_pairs
        self.assertEqual(
            iter_pw_link_pairs(link_line("a:out", "b:in", 7)),
            [("a:out", "b:in")])
        self.assertEqual(iter_pw_link_pairs(""), [])


class LinkVerificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MeasurementStore(home=pathlib.Path(self._tmp.name))
        self.routing = self.store._routing
        self.process = Mock()
        self.process.poll.return_value = None
        self.runs = []

    def _link(self, listing=("",), **overrides):
        """Drive the real host-reference linker with scripted pw-link -l."""
        listings = list(listing)

        def fake_run(cmd, **kwargs):
            self.runs.append(list(cmd))
            if cmd[:2] == ["pw-link", "-l"]:
                return SimpleNamespace(returncode=0, stdout=listings.pop(0) if listings else "")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        options = dict(
            reference_source_node_name="fxroute_dsp_sink.monitor",
            mic_source_node_name="mic",
            record_node_name="record",
            requested_channel="left",
            record_process=self.process,
        )
        options.update(overrides)
        ports = {
            "fxroute_dsp_sink.monitor": ["fxroute_dsp_sink.monitor:monitor_FL"],
            "mic": ["mic:capture_FL"],
        }
        inputs = ["record:input_FL", "record:input_FR"]
        with patch.object(self.routing, "_list_source_output_ports",
                           side_effect=lambda name: ports[name]), \
             patch.object(self.store, "_list_pw_ports", return_value=inputs), \
             patch.object(self.routing, "_run", side_effect=fake_run), \
             patch("measurement.routing.time.sleep", return_value=None):
            return self.routing._link_host_reference_capture(**options)

    def test_phantom_links_fail_closed_for_microphone(self):
        """pw-link succeeding without the link appearing must abort, not sweep."""
        with self.assertRaisesRegex(RuntimeError, "microphone"):
            self._link(listing=("", "", ""))
        create_calls = [c for c in self.runs if len(c) > 2 and c[2] != "-l"]
        self.assertEqual(len(create_calls), 2)

    def test_links_appearing_late_proceed(self):
        """Links materializing after a few polls proceed with evidence."""
        result = self._link(listing=("", tree_block(
            "fxroute_dsp_sink.monitor:monitor_FL", "record:input_FL") + "\n" + tree_block(
            "mic:capture_FL", "record:input_FR")))
        verified = result.get("link_verified") or {}
        self.assertEqual(len(verified), 2)
        self.assertTrue(all(isinstance(value, float) for value in verified.values()))

    def test_missing_reference_only_warns(self):
        """A missing monitor link warns (analysis still runs); mic is fatal."""
        result = self._link(listing=(tree_block("mic:capture_FL", "record:input_FR"),) * 3)
        self.assertIn("link_warning", result)
        self.assertIn("reference", result["link_warning"])


class CleanupScopingTests(unittest.TestCase):
    """Per-capture cleanup must not destroy unrelated taps (e.g. keepers)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MeasurementStore(home=pathlib.Path(self._tmp.name))
        self.routing = self.store._routing

    def cleanup(self, listing):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["pw-link", "-l"]:
                return SimpleNamespace(returncode=0, stdout=listing)
            raise AssertionError(f"unexpected command {cmd}")

        disconnected = []

        def fake_disconnect(dst, src):
            disconnected.append((dst, src))
            return True

        with patch.object(self.routing, "_run", side_effect=fake_run), \
             patch.object(self.store, "_disconnect_link", side_effect=fake_disconnect):
            removed = self.routing._cleanup_fxroute_links(
                source_node_name="mic", record_node_name="record")
        return removed, disconnected

    def test_stale_pair_links_are_removed(self):
        removed, disconnected = self.cleanup("\n".join([
            tree_block("mic:capture_FL", "record:input_FR"),
            tree_block("fxroute_dsp_sink.monitor:monitor_FL", "record:input_FL"),
        ]))
        self.assertEqual(len(disconnected), 2)
        self.assertEqual(len(removed), 2)

    def test_keeper_and_foreign_links_survive(self):
        """Links touching the mic source but another record node are kept."""
        removed, disconnected = self.cleanup("\n".join([
            tree_block("mic:capture_FL", "record:input_FR"),
            tree_block("mic:capture_FL", "keeper:input_AUX0"),
            tree_block("other:out", "elsewhere:in"),
        ]))
        self.assertEqual(disconnected, [("record:input_FR", "mic:capture_FL")])
        self.assertEqual(len(removed), 1)


if __name__ == "__main__":
    unittest.main()
