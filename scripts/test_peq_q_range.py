#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""PEQ Q above 20 (real REW exports) up to the LSP Q-port bound of 100.

The value a REW import carries must be stored and rendered exactly: in the
area-bank native biquads and in the Global bank's LSP equalizer controls.
Every validator shares one bound; 100 is accepted, anything above fails.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp import native_config
from dsp.manager import DSPManager
from dsp.native_config import PEQ_Q_MAX, PEQ_Q_MIN, layout_from_plan
from dsp.processing_plan import compile_processing_plan
from measurement.autosub.jobs import _auto_sub_validated_layout_output

# Real REW exports carry Q 31.69 or 45.76; the low Q band must stay as is.
REW = (
    "Filter Settings file\n"
    "Equaliser: Generic\n"
    "Filter  1: ON  PK       Fc   42.80 Hz  Gain  -6.10 dB  Q 31.000\n"
    "Filter  2: ON  PK       Fc     688 Hz  Gain   7.00 dB  Q 45.760\n"
    "Filter  3: ON  PK       Fc     209 Hz  Gain  -3.90 dB  Q  0.707\n"
)
EXPECTED = [(42.8, 31.0), (688.0, 45.76), (209.0, 0.707)]


class PeqQRangeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name))
        self.name = self.manager.create_peq_preset_from_rew_text("High Q", REW)["name"]

    def engine_text(self, bank):
        state = default_output_state()
        state = switch_mode(set_mode_routing(
            state, "stereo-sub", "A", ["main_l", "main_r", "sub1"]), "stereo-sub")
        state["modes"]["stereo-sub"]["banks"][bank]["preset"] = self.name
        plan = compile_processing_plan(state, output_key="A", channels=3, sample_rate_hz=48000,
                                       preset_loader=self.manager.preset_store.read)
        layout = layout_from_plan(plan, resolve_ir=lambda name: {})
        text = self.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=48000,
            extras_override=plan["global"]["extras"])
        return layout, text.splitlines()

    def test_rew_import_stores_q_above_20_exactly(self):
        bands = self.manager.preset_store.read(self.name)["chain"][0]["params"]["bands"]
        self.assertEqual([(band["frequencyHz"], band["q"]) for band in bands], EXPECTED)

    def test_area_bank_renders_the_imported_q_in_its_biquads(self):
        layout, lines = self.engine_text("main_l")
        row = next(row for row in layout if row["name"] == "main_l")
        self.assertEqual([(item["frequency_hz"], item["q"]) for item in row["filters"]], EXPECTED)
        for frequency, q in EXPECTED:
            self.assertIn(f"peq 0 bell {frequency:.9g} {q:.9g}", "\n".join(lines))

    def test_global_bank_renders_the_imported_q_in_the_lsp_controls(self):
        _, lines = self.engine_text("global")
        for index, (_, q) in enumerate(EXPECTED):
            for side in ("l", "r"):
                self.assertIn(f"control q{side}_{index} {q:.9g}", lines)

    def test_every_validator_shares_the_bound_and_rejects_beyond_it(self):
        self.assertEqual((PEQ_Q_MIN, PEQ_Q_MAX), (0.1, 100.0))

        def peq(q):
            self.manager.validate_peq_v1({"params": {"bands": [
                {"filterType": "bell", "frequencyHz": 100, "gainDb": -3, "q": q}]}})

        def native_band(q):
            native_config._validate_band(
                {"filterType": "bell", "frequencyHz": 100, "gainDb": -3, "q": q}, 0)

        def output_filter(q):
            self.manager._validate_output_filters(
                [{"type": "bell", "frequency_hz": 100, "q": q, "gain_db": -3}], 0, 48000)

        def auto_sub_layout(q):
            _auto_sub_validated_layout_output(
                {"routes": [{"input": 0, "gain": 1.0}], "filters": [
                    {"type": "bell", "frequency_hz": 100, "q": q, "gain_db": -3}]}, 0, 48000)

        for validate in (peq, native_band, output_filter, auto_sub_layout):
            with self.subTest(validator=validate.__name__):
                for q in (PEQ_Q_MIN, 20.0, 31.0, 45.76, PEQ_Q_MAX):
                    validate(q)
                for q in (0.09, 100.5, float("inf")):
                    with self.assertRaises(ValueError, msg=f"q={q}"):
                        validate(q)


if __name__ == "__main__":
    unittest.main()
