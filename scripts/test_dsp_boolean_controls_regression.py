#!/usr/bin/env python3
"""Regression: all DSP/output boolean controls must survive Off/false/invert.

Same Fehlertyp wie main_highpass_enabled (fix 3b7c3b8): doppelte Felder,
stale Top-Level, Render-Overwrite, false-Verlust im Merge.

Covers without full suite:
  1. Subwoofer Highpass-Select hat denselben _activeEditing-Guard wie die
     anderen 7 Subwoofer-Controls (sonst überschreibt renderSubwooferPanel
     den gerade gewählten Wert vor dem Save).
  2. Polarität invert für einen und zwei Subs (v2-Plan + Layout invert-Flag).
  3. Extras-Merge erhält explizites false für alle 6 Toggles.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.effects_extras import merge_effects_extras_from_json
from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan


def _planned_layout(routing: list[str], processing: dict | None = None,
                    bass: dict | None = None) -> dict:
    """Native layout rows by role for a Stereo + Sub state on one device."""
    state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "k", routing), "stereo-sub")
    mode = state["modes"]["stereo-sub"]
    for role, values in (processing or {}).items():
        mode["processing"][role].update(values)
    mode["bass_management"].update(bass or {})
    plan = compile_processing_plan(state, output_key="k", channels=len(routing), sample_rate_hz=48000,
                                   preset_loader=lambda *_args: {"chain": []})
    return {row["name"]: row for row in layout_from_plan(plan, resolve_ir=lambda *_args: {})}


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
    # Single sub inverted.
    single = _planned_layout(["main_l", "main_r", "sub1"], {"sub1": {"polarity": "invert"}})
    assert single["sub1"].get("invert") is True, single["sub1"]
    # Two subs: sub1 inverted, sub2 normal.
    dual = _planned_layout(["main_l", "main_r", "sub1", "sub2"],
                           {"sub1": {"polarity": "invert"}, "sub2": {"polarity": "normal"}})
    assert dual["sub1"].get("invert") is True, dual["sub1"]
    assert dual["sub2"].get("invert") is False, dual["sub2"]
    print("polarity invert single + dual sub reaches the planned layout: ok")


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
