#!/usr/bin/env python3
"""Regression: DSP -> Subwoofer tile -> Main highpass Off must stick.

Covers the overview payload path and the live UI save path: 2.1/2.2
payloads keep main_highpass_enabled=false into
BassManagementConfig/DSP runtime layout (no FL/FR highpass filters),
while On still produces them; the sub tile saves through the single v2
output state (set_subwoofers), never a legacy output-mode POST.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.runtime import BassManagementConfig, DSPRuntimeConfig


def _with_temp_config_home(fn):
    with tempfile.TemporaryDirectory() as raw:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(raw)
        try:
            fn(Path(raw))
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def _check_21_off_roundtrip() -> None:
    def run(_: Path) -> None:
        sub_off = {
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": False,
            "sub_level_db": 0.0,
            "sub_alignment_ms": 0.0,
            "sub_polarity": "normal",
        }
        loaded = {"mode": "subwoofer-2.1",
                  "subwoofer": {**dict(sub_off), "slope": "LR24"}}
        assert loaded["subwoofer"]["main_highpass_enabled"] is False, loaded

        overview = {"output_mode": loaded, "selected_output": {"key": "k", "label": "l"},
                    "current_output": {"key": "k"}}
        bass = BassManagementConfig.from_overview(overview)
        assert bass.main_highpass_enabled is False, bass
        runtime_cfg = DSPRuntimeConfig.from_overview(overview)
        for channel in runtime_cfg.layout:
            if channel["name"] in ("FL", "FR"):
                assert channel.get("filters", []) == [], channel
        # On must still produce highpass filters.
        sub_on = {**dict(sub_off), "main_highpass_enabled": True, "slope": "LR24"}
        assert sub_on["main_highpass_enabled"] is True, sub_on
        overview_on = {"output_mode": {**loaded, "subwoofer": sub_on},
                       "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        bass_on = BassManagementConfig.from_overview(overview_on)
        assert bass_on.main_highpass_enabled is True, bass_on
        runtime_on = DSPRuntimeConfig.from_overview(overview_on)
        fl = next(c for c in runtime_on.layout if c["name"] == "FL")
        assert any(f.get("type") == "highpass" for f in fl.get("filters", [])), fl

    _with_temp_config_home(run)
    print("2.1 Off round-trip + runtime (no highpass filters): ok")


def _check_22_off_roundtrip() -> None:
    def run(_: Path) -> None:
        sub_off = {
            "crossover_frequency_hz": 80,
            "slope": "LR24",
            "main_highpass_enabled": False,
            "sub_level_db": 1.5,
            "sub_alignment_ms": 0.5,
            "sub_polarity": "normal",
        }
        subwoofers = {
            "sub1": {"level_db": 1.5, "alignment_ms": 0.5, "polarity": "normal"},
            "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
        }
        loaded = {"mode": "subwoofer-2.2", "crossover_frequency_hz": 80,
                  "slope": "LR24", "main_highpass_enabled": False,
                  "subwoofers": dict(subwoofers)}
        assert loaded["main_highpass_enabled"] is False, loaded
        assert loaded["crossover_frequency_hz"] == 80, loaded

        overview = {"output_mode": loaded, "selected_output": {"key": "k", "label": "l"},
                    "current_output": {"key": "k"}}
        bass = BassManagementConfig.from_overview(overview)
        assert bass.main_highpass_enabled is False, bass
        runtime_cfg = DSPRuntimeConfig.from_overview(overview)
        for channel in runtime_cfg.layout:
            if channel["name"] in ("FL", "FR"):
                assert channel.get("filters", []) == [], channel
        # Sub lowpass must remain regardless of highpass toggle.
        subs = [c for c in runtime_cfg.layout if c["name"].startswith("SUB")]
        assert subs and all(
            any(f.get("type") == "lowpass" for f in c.get("filters", [])) for c in subs), subs

        # On must still produce highpass filters.
        sub_on = dict(sub_off, main_highpass_enabled=True)
        assert sub_on["main_highpass_enabled"] is True, sub_on
        overview_on = {"output_mode": {**loaded, "main_highpass_enabled": True},
                       "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        runtime_on = DSPRuntimeConfig.from_overview(overview_on)
        fl = next(c for c in runtime_on.layout if c["name"] == "FL")
        assert any(f.get("type") == "highpass" for f in fl.get("filters", [])), fl

    _with_temp_config_home(run)
    print("2.2 Off round-trip + runtime (no highpass filters): ok")


def _check_frontend_draft_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "static" / "app.js").read_text()
    # The sub tile saves through the single v2 state (no legacy output-mode POST):
    # routed roles map to set_subwoofers with exact sub processing.
    assert "set_subwoofers" in text, "sub tile must save via set_subwoofers"
    assert "routedSubwooferView" in text, "sub tile must read routed subs"
    assert "subwooferView" in (root / "static" / "output_state.js").read_text(), \
        "sub view adapter missing"
    print("frontend sub save uses single v2 state: ok")


def main() -> None:
    _check_21_off_roundtrip()
    _check_22_off_roundtrip()
    _check_frontend_draft_sync()
    print("main highpass Off regression tests: ok")


if __name__ == "__main__":
    main()
