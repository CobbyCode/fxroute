#!/usr/bin/env python3
"""Regression: DSP -> Subwoofer tile -> Main highpass Off must stick.

Covers the v2 plan path and the live UI save path: single and dual sub
states keep main_highpass_enabled=false into the planned native layout
(no main high-pass sections), while On still produces them; the sub tile
saves through the single v2 output state (set_subwoofers), never a legacy
output-mode POST.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _check_off_roundtrip(routing: list[str], label: str) -> None:
    off = _planned_layout(routing, bass={"main_highpass_enabled": False})
    for role in ("main_l", "main_r"):
        assert not off[role].get("sos"), off[role]
    # Sub low-pass must remain regardless of the main high-pass switch.
    for role in routing[2:]:
        assert off[role].get("sos"), off[role]
    # On must still produce the main high-pass.
    on = _planned_layout(routing, bass={"main_highpass_enabled": True})
    for role in ("main_l", "main_r"):
        assert on[role].get("sos"), on[role]
    print(f"{label} main high-pass Off reaches the planned layout: ok")


def _check_frontend_draft_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    app = (root / "static" / "app.js").read_text()
    text = (root / "static" / "subwoofer_ui.js").read_text()
    # The sub tile saves through the single v2 state (no legacy output-mode POST):
    # routed roles map to set_subwoofers with exact sub processing.
    assert "set_subwoofers" in text, "sub tile must save via set_subwoofers"
    assert "routedSubwooferView" in text, "sub tile must read routed subs"
    assert "FXRouteSubwooferUI" in app, "app.js must delegate subwoofer UI to the module"
    assert "subwooferView" in (root / "static" / "output_state.js").read_text(), \
        "sub view adapter missing"
    print("frontend sub save uses single v2 state: ok")


def main() -> None:
    _check_off_roundtrip(["main_l", "main_r", "sub1"], "single sub")
    _check_off_roundtrip(["main_l", "main_r", "sub1", "sub2"], "two subs")
    _check_frontend_draft_sync()
    print("main highpass Off regression tests: ok")


if __name__ == "__main__":
    main()
