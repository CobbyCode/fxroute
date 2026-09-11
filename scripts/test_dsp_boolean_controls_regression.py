#!/usr/bin/env python3
"""Regression: all DSP/output boolean controls must survive Off/false/invert.

Same Fehlertyp wie main_highpass_enabled (fix 3b7c3b8): doppelte Felder,
stale Top-Level, Render-Overwrite, false-Verlust im Merge.

Covers without full suite:
  1. Subwoofer Highpass-Select hat denselben _activeEditing-Guard wie die
     anderen 7 Subwoofer-Controls (sonst überschreibt renderSubwooferPanel
     den gerade gewählten Wert vor dem Save).
  2. Polarität invert round-trip 2.1 + 2.2 (persist + runtime invert-Flag).
  3. Extras-Merge erhält explizites false für alle 6 Toggles.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.samplerate import persistence as persistence
from dsp.effects_extras import merge_effects_extras_from_json
from dsp.runtime import BassManagementConfig, DSPRuntimeConfig


def _with_temp_home(fn) -> None:
    with tempfile.TemporaryDirectory() as raw:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(raw)
        try:
            fn()
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def _check_highpass_guard() -> None:
    text = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    guarded = [
        "effectsSubwooferFrequencyNumber",
        "effectsSubwooferMainHighpass",
        "effectsSubwooferLevel",
        "effectsSubwooferDelay",
        "effectsSubwooferPolarity",
        "effectsSubwooferSub2Level",
        "effectsSubwooferSub2Delay",
        "effectsSubwooferSub2Polarity",
    ]
    for name in guarded:
        needle = f"_activeEditing.has(elements.{name})"
        assert needle in text, f"missing _activeEditing guard for {name}"
    # Highpass-Render darf nicht guard-los schreiben.
    assert "if (elements.effectsSubwooferMainHighpass && !_activeEditing.has(elements.effectsSubwooferMainHighpass))" in text
    print("highpass select has _activeEditing guard like the other 7: ok")


def _check_polarity() -> None:
    def run() -> None:
        path = persistence._audio_output_mode_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 2.1 invert
        built = persistence._build_audio_output_mode_payload(
            "subwoofer-2.1",
            {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
             "sub_level_db": 0.0, "sub_alignment_ms": 0.0, "sub_polarity": "invert"},
            None,
        )
        assert built["subwoofer"]["sub_polarity"] == "invert", built
        path.write_text(json.dumps(built, indent=2) + "\n")
        loaded = persistence._load_audio_output_mode()
        assert loaded["subwoofer"]["sub_polarity"] == "invert", loaded
        ov = {"output_mode": loaded, "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        rt = DSPRuntimeConfig.from_overview(ov)
        subs = [c for c in rt.layout if c["name"].startswith("SUB")]
        assert subs and all(c.get("invert") is True for c in subs), subs
        # 2.2 sub1=invert, sub2=normal
        sub = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
               "sub_level_db": 0.0, "sub_alignment_ms": 0.0, "sub_polarity": "invert"}
        subs22 = {"sub1": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "invert"},
                  "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"}}
        built22 = persistence._build_audio_output_mode_payload("subwoofer-2.2", dict(sub), dict(subs22))
        path.write_text(json.dumps(built22, indent=2) + "\n")
        loaded22 = persistence._load_audio_output_mode()
        assert loaded22["subwoofers"]["sub1"]["polarity"] == "invert", loaded22
        assert loaded22["subwoofers"]["sub2"]["polarity"] == "normal", loaded22
        ov22 = {"output_mode": loaded22, "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        rt22 = DSPRuntimeConfig.from_overview(ov22)
        by_name = {c["name"]: c for c in rt22.layout}
        assert by_name["SUB1"].get("invert") is True, by_name["SUB1"]
        assert by_name["SUB2"].get("invert") is False, by_name["SUB2"]

    _with_temp_home(run)
    print("polarity invert round-trip 2.1 + 2.2 + runtime: ok")


def _check_extras_false() -> None:
    prev = {
        "limiter": {"enabled": True},
        "headroom": {"enabled": True, "params": {"gainDb": -3}},
        "autogain": {"enabled": True},
        "loudness": {"enabled": True},
        "bass_enhancer": {"enabled": True},
        "tone_effect": {"enabled": True, "mode": "crystalizer"},
    }
    bodies = [
        {"limiterEnabled": False},
        {"headroomEnabled": False},
        {"autogainEnabled": False},
        {"loudnessEnabled": False},
        {"bassEnabled": False},
        {"toneEffectEnabled": False},
    ]
    sections = ["limiter", "headroom", "autogain", "loudness", "bass_enhancer", "tone_effect"]
    for body, section in zip(bodies, sections):
        merged = merge_effects_extras_from_json(prev, body)
        assert merged[section]["enabled"] is False, (body, merged[section])
    merged_all = merge_effects_extras_from_json(prev, {
        "limiterEnabled": False, "headroomEnabled": False, "autogainEnabled": False,
        "loudnessEnabled": False, "bassEnabled": False, "toneEffectEnabled": False,
    })
    assert all(merged_all[s]["enabled"] is False for s in sections), merged_all
    print("extras merge keeps explicit false for all 6 toggles: ok")


def main() -> None:
    _check_highpass_guard()
    _check_polarity()
    _check_extras_false()
    print("dsp boolean controls regression tests: ok")


if __name__ == "__main__":
    main()
