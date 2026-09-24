#!/usr/bin/env python3
"""Regression: all DSP/output boolean controls must survive Off/false/invert.

Same Fehlertyp wie main_highpass_enabled (fix 3b7c3b8): doppelte Felder,
stale Top-Level, Render-Overwrite, false-Verlust im Merge.

Covers without full suite:
  1. Subwoofer Highpass-Select hat denselben _activeEditing-Guard wie die
     anderen 7 Subwoofer-Controls (sonst überschreibt renderSubwooferPanel
     den gerade gewählten Wert vor dem Save).
  2. Polarität invert round-trip 2.1 + 2.2 (payload + runtime invert-Flag).
  3. Extras-Merge erhält explizites false für alle 6 Toggles.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.effects_extras import merge_effects_extras_from_json
from dsp.runtime import DSPRuntimeConfig


def _block(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start)]


def _check_highpass_guard() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "static" / "subwoofer_ui.js").read_text()
    app = (root / "static" / "app.js").read_text()
    assert "FXRouteSubwooferUI" in app, "app.js must delegate subwoofer UI to the module"
    # Frequency/type/slope of the shared and both per-side groups are painted
    # by one shape renderer; it skips a control the user is editing, so the
    # render can never overwrite a value before its save ran.
    for control in ("effectsSubwooferFrequencyNumber", "effectsSubwooferLeftFrequency",
                    "effectsSubwooferRightFrequency"):
        assert f"applySubCrossoverShape(deps.getElements().{control}," in text, f"{control} is not shape-rendered"
    assert "if (frequencyEl && !_activeEditing.has(frequencyEl))" in text
    # Level/alignment/polarity keep their own per-control guards.
    for name in ("effectsSubwooferLevel", "effectsSubwooferDelay", "effectsSubwooferPolarity",
                 "effectsSubwooferSub2Level", "effectsSubwooferSub2Delay",
                 "effectsSubwooferSub2Polarity"):
        assert f"!_activeEditing.has(deps.getElements().{name})" in text, f"missing _activeEditing guard for {name}"
    # The global Main high-pass flag paints all three copies under the guard.
    main_highpass = _block(text, "function applySubMainHighpass(", "function subMainHighpassEnabled(")
    assert "!_activeEditing.has(el)" in main_highpass
    for name in ("effectsSubwooferMainHighpass", "effectsSubwooferLeftMainHighpass",
                 "effectsSubwooferRightMainHighpass"):
        assert f"deps.getElements().{name}" in main_highpass, f"{name} is not repainted by the shared flag"
    # Every subwoofer control (shared, per-side, level and the link switch) is
    # released from the guard after its save, so edits do not stick forever.
    released = _block(text, "function clearSubwooferActiveEditing()", "function getSubwooferPreviewLayout(")
    for name in ("effectsSubwooferFrequencyNumber", "effectsSubwooferFamily", "effectsSubwooferSlope",
                 "effectsSubwooferLeftFrequency", "effectsSubwooferLeftFamily", "effectsSubwooferLeftSlope",
                 "effectsSubwooferRightFrequency", "effectsSubwooferRightFamily",
                 "effectsSubwooferRightSlope", "effectsSubwooferLink", "effectsSubwooferMainHighpass",
                 "effectsSubwooferLeftMainHighpass", "effectsSubwooferRightMainHighpass",
                 "effectsSubwooferLevel", "effectsSubwooferDelay", "effectsSubwooferPolarity",
                 "effectsSubwooferSub2Level", "effectsSubwooferSub2Delay",
                 "effectsSubwooferSub2Polarity"):
        assert f"deps.getElements().{name}," in released, f"{name} is never released from the render guard"
    print("all subwoofer controls render under the _activeEditing guard: ok")


def _check_polarity() -> None:
    def run() -> None:
        # 2.1 invert
        sub_21 = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
                  "sub_level_db": 0.0, "sub_alignment_ms": 0.0, "sub_polarity": "invert"}
        loaded = {"mode": "subwoofer-2.1",
                  "subwoofer": {**sub_21, "slope": "LR24"}}
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
        loaded22 = {"mode": "subwoofer-2.2", "crossover_frequency_hz": 80,
                    "slope": "LR24", "main_highpass_enabled": True,
                    "subwoofers": dict(subs22)}
        assert loaded22["subwoofers"]["sub1"]["polarity"] == "invert", loaded22
        assert loaded22["subwoofers"]["sub2"]["polarity"] == "normal", loaded22
        ov22 = {"output_mode": loaded22, "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        rt22 = DSPRuntimeConfig.from_overview(ov22)
        by_name = {c["name"]: c for c in rt22.layout}
        assert by_name["SUB1"].get("invert") is True, by_name["SUB1"]
        assert by_name["SUB2"].get("invert") is False, by_name["SUB2"]

    run()
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
