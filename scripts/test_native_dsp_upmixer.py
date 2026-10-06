#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Surround upmixer stage in the native engine, driven by compiled engine configs."""

import array
import math
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dsp.manager import DSPManager
from dsp.native_config import layout_from_plan
import test_surround as surround_support

DEPS = ["libebur128", "lilv-0", "samplerate", "speexdsp"]
AVAILABLE = subprocess.run(["pkg-config", "--exists", *DEPS], check=False).returncode == 0
RATE = 48000
LATENCY = 64
ON = {"limiter": {"enabled": False},
      "upmixer": {"enabled": True, "params": {"mode": "fast", "center": 10, "surround": 10}}}
OFF = {"limiter": {"enabled": False}}


def power(values, begin, count):
    return sum(v * v for v in values[begin:begin + count]) / count


@unittest.skipUnless(AVAILABLE, "missing native DSP build dependencies; build on test host")
class NativeUpmixerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["sh", str(ROOT / "native_dsp/build.sh")], check=True, capture_output=True)
        cls.surround = surround_support.SurroundTests()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = DSPManager(Path(self.directory.name) / "presets", Path(self.directory.name) / "irs")

    def render(self, roles, extras, left, right, text_filter=None):
        """Compile for the routing, run the offline engine and return outputs by role name."""
        layout = layout_from_plan(self.surround.plan(self.surround.state(roles)), resolve_ir=lambda name: {})
        text = self.manager.compile_engine_text(layout, preset_name="Neutral", sample_rate_hz=RATE,
                                                extras_override=extras)
        if text_filter:
            text = text_filter(text)
        frames = array.array("f")
        for l, r in zip(left, right):
            frames.extend([l, r, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        path = Path(self.directory.name)
        (path / "config").write_text(text)
        (path / "input").write_bytes(frames.tobytes())
        subprocess.run([str(ROOT / "native_dsp/build/fxroute-dsp-offline"),
                        str(path / "config"), str(path / "input"), str(path / "output"), "509"],
                       check=True, capture_output=True)
        data = array.array("f")
        data.frombytes((path / "output").read_bytes())
        return {item["name"]: data[index::len(layout)] for index, item in enumerate(layout)}

    @staticmethod
    def noise(frames, seed, rms=0.1):
        rng = random.Random(seed)
        return [rng.gauss(0.0, rms) for _ in range(frames)]

    def test_decorrelated_stereo_reaches_the_rear_pair_without_a_center(self):
        frames = 3 * RATE
        left, right = self.noise(frames, 1), self.noise(frames, 2)
        on = self.render(surround_support.QUAD_SUBS, ON, left, right)
        off = self.render(surround_support.QUAD_SUBS, OFF, left, right)
        self.assertNotIn("center", on)
        # Without the upmixer a stereo programme leaves the rear pair silent.
        self.assertEqual(power(off["rear_l"], 0, frames), 0.0)
        for name in ("rear_l", "rear_r"):
            self.assertGreater(power(on[name], 2 * RATE, RATE), 0.2 * power(left, 2 * RATE, RATE), name)

    def test_centre_image_front_and_bass_are_unchanged_without_a_center(self):
        frames = 3 * RATE
        mono = [0.1 * math.sin(2 * math.pi * 60 * n / RATE) + noise
                for n, noise in enumerate(self.noise(frames, 3, 0.05))]
        on = self.render(surround_support.QUAD_SUBS, ON, mono, mono)
        off = self.render(surround_support.QUAD_SUBS, OFF, mono, mono)
        for name in ("main_l", "main_r", "sub1", "sub2"):
            shifted = on[name][LATENCY:LATENCY + frames - LATENCY - 4096]
            reference = off[name][:frames - LATENCY - 4096]
            self.assertLess(max(abs(a - b) for a, b in zip(shifted, reference)), 1e-5, name)
        for name in ("rear_l", "rear_r"):
            self.assertLess(power(on[name], 0, frames), 1e-12, name)

    def test_center_output_receives_the_extracted_centre_and_fronts_give_it_up(self):
        frames = 3 * RATE
        mono = self.noise(frames, 4)
        on = self.render(surround_support.ROLES, ON, mono, mono)
        off = self.render(surround_support.ROLES, OFF, mono, mono)
        self.assertEqual(power(off["center"], 0, frames), 0.0)
        self.assertGreater(power(on["center"], 2 * RATE, RATE), 1.5 * power(mono, 2 * RATE, RATE))
        self.assertLess(power(on["main_l"], 2 * RATE, RATE), 0.01 * power(mono, 2 * RATE, RATE))

    def test_zero_strength_is_accepted_and_silences_only_its_own_share(self):
        frames = 3 * RATE
        common = self.noise(frames, 5)
        left = [c + n for c, n in zip(common, self.noise(frames, 6))]
        right = [c + n for c, n in zip(common, self.noise(frames, 7))]
        reference = power(left, 2 * RATE, RATE)

        def render(center, surround):
            extras = {"limiter": {"enabled": False}, "upmixer": {
                "enabled": True, "params": {"mode": "fast", "center": center, "surround": surround}}}
            return self.render(surround_support.ROLES, extras, left, right)

        centre_only, surround_only, neither = render(10, 0), render(0, 10), render(0, 0)
        # Center 10 / Surround 0: the centre is fed, every surround pair stays silent.
        self.assertGreater(power(centre_only["center"], 2 * RATE, RATE), 0.3 * reference)
        for name in ("side_l", "side_r", "rear_l", "rear_r"):
            self.assertEqual(power(centre_only[name], 0, frames), 0.0, name)
        # Center 0 / Surround 10: the centre stays silent, the surround pairs are fed.
        self.assertEqual(power(surround_only["center"], 0, frames), 0.0)
        for name in ("side_l", "rear_l"):
            self.assertGreater(power(surround_only[name], 2 * RATE, RATE), 0.02 * reference, name)
        # Both 0: the programme passes through the same fixed delay and nothing else.
        for name in ("center", "side_l", "side_r", "rear_l", "rear_r"):
            self.assertEqual(power(neither[name], 0, frames), 0.0, name)
        off = self.render(surround_support.ROLES, OFF, left, right)
        for name in ("main_l", "main_r"):
            shifted = neither[name][LATENCY:LATENCY + frames - LATENCY - 4096]
            self.assertLess(max(abs(a - b) for a, b in zip(shifted, off[name][:frames - LATENCY - 4096])), 1e-5, name)

    def test_zero_surround_keeps_the_rear_pair_silent_without_a_center(self):
        frames = 3 * RATE
        left, right = self.noise(frames, 8), self.noise(frames, 9)
        extras = {"limiter": {"enabled": False}, "upmixer": {
            "enabled": True, "params": {"mode": "fast", "center": 10, "surround": 0}}}
        result = self.render(surround_support.QUAD_SUBS, extras, left, right)
        for name in ("rear_l", "rear_r"):
            self.assertEqual(power(result[name], 0, frames), 0.0, name)
        # Surround 10 with Center 0 on the same routing still works.
        extras["upmixer"]["params"].update({"center": 0, "surround": 10})
        result = self.render(surround_support.QUAD_SUBS, extras, left, right)
        self.assertGreater(power(result["rear_l"], 2 * RATE, RATE), 0.2 * power(left, 2 * RATE, RATE))

    def test_stage_requires_surround_and_rejects_bad_parameters(self):
        frames = 4800
        silence = [0.0] * frames

        def failing(text_filter):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                self.render(surround_support.QUAD_SUBS, ON, silence, silence, text_filter)
            return raised.exception.stderr.decode() if isinstance(raised.exception.stderr, bytes) else raised.exception.stderr

        self.assertIn("requires Surround", failing(lambda text: text.replace("surround 1\n", "")))
        self.assertIn("invalid config line", failing(lambda text: text.replace("param center 10", "param center 11")))
        self.assertIn("invalid config line", failing(lambda text: text.replace("param surround 10", "param surround -1")))
        self.assertIn("invalid config line", failing(lambda text: text.replace("param mode fast", "param mode hq")))
        self.assertIn("invalid config line", failing(lambda text: text.replace("param rear_out 1", "param rear_out 2")))


if __name__ == "__main__":
    unittest.main()
