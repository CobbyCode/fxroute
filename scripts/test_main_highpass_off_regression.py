#!/usr/bin/env python3
"""Regression: DSP -> Crossover/Subwoofer -> Main highpass Off must stick.

Covers the full path for 2.1 and 2.2:
  UI draft (2.2 top-level sync) -> API payload build -> persisted state
  -> BassManagementConfig/DSP runtime layout -> readback/render source.

Root cause fixed here: for 2.2 the UI draft only updated
`output_mode.subwoofer` + `output_mode.subwoofers`, leaving the stale
top-level `main_highpass_enabled=true`. getSubwooferGlobalSettings() prefers
the top-level field, so renderSubwooferPanel() snapped the select back to On
on `input`, and the following `change` save re-read On. Off never reached
the API. The draft now mirrors crossover/highpass to top-level (see
applySubwooferDraftToOutputMode in static/app.js).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.samplerate import persistence as persistence
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
        built = persistence._build_audio_output_mode_payload("subwoofer-2.1", dict(sub_off), None)
        assert built["subwoofer"]["main_highpass_enabled"] is False, built
        path = persistence._audio_output_mode_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(built, indent=2) + "\n")
        loaded = persistence._load_audio_output_mode()
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
        sub_on = dict(sub_off, main_highpass_enabled=True)
        built_on = persistence._build_audio_output_mode_payload("subwoofer-2.1", sub_on, None)
        assert built_on["subwoofer"]["main_highpass_enabled"] is True, built_on
        overview_on = {"output_mode": {**loaded, "subwoofer": built_on["subwoofer"]},
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
        built = persistence._build_audio_output_mode_payload(
            "subwoofer-2.2", dict(sub_off), dict(subwoofers))
        assert built["main_highpass_enabled"] is False, built
        path = persistence._audio_output_mode_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(built, indent=2) + "\n")
        loaded = persistence._load_audio_output_mode()
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
        built_on = persistence._build_audio_output_mode_payload(
            "subwoofer-2.2", sub_on, dict(subwoofers))
        assert built_on["main_highpass_enabled"] is True, built_on
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
    assert "function applySubwooferDraftToOutputMode" in text, "draft helper missing"
    # The helper must mirror the 2.2 globals to top-level; otherwise the
    # 2.2 draft keeps a stale top-level true and the select snaps back to On.
    assert "main_highpass_enabled: settings.subwoofer" in text or \
        "main_highpass_enabled\" ] = settings.subwoofer" in text or \
        "next.main_highpass_enabled = settings.subwoofer.main_highpass_enabled" in text, \
        "draft helper does not sync main_highpass_enabled to top-level"
    assert "crossover_frequency_hz" in text
    # All three optimistic/draft sites must use the helper (no stale spread left).
    assert text.count("applySubwooferDraftToOutputMode(") >= 3, text.count("applySubwooferDraftToOutputMode(")
    print("frontend 2.2 draft sync helper present and wired: ok")


def main() -> None:
    _check_21_off_roundtrip()
    _check_22_off_roundtrip()
    _check_frontend_draft_sync()
    print("main highpass Off regression tests: ok")


if __name__ == "__main__":
    main()
