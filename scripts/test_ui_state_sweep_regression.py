#!/usr/bin/env python3
"""Regression: UI-state sweep findings (pre-beta audit).

  1. 2.2 sub level -80 dB round-trip: AutoSub mutes inactive 2.2 subs to
     -80 (backend/runtime accept -80..12). The frontend clamp (-24) showed
     -24 and re-saved -24 (+56 dB unmute). Frontend now keeps -80.
  2. Convolver draft invalidation: changing irLength (incl. the legacy
     quality path) after Take L/R left a stale draft; Create silently used
     the old taps. The change now invalidates the draft.
  3. Headroom range: backend accepts whole dB -9..0, UI offered only -1..-6,
     so stored 0/-7/-8/-9 rendered as -3 and re-saved as -3. UI now spans
     the backend contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan

ROOT = Path(__file__).resolve().parents[1]


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


def _check_sub_mute_minus80() -> None:
    text = (ROOT / "static" / "subwoofer_ui.js").read_text()
    app = (ROOT / "static" / "app.js").read_text()
    assert "FXRouteSubwooferUI" in app, "app.js must delegate subwoofer UI to the module"
    assert "Math.max(-80, Math.min(12, Number(input.level_db" in text, \
        "normalizeSingleSubwooferSettings must keep the -80 mute floor"
    assert "Math.max(-80, Math.min(12, Number(input.sub_level_db" in text, \
        "normalizeSubwooferSettings must keep the -80 mute floor for the 2.2 global display"

    layout = _planned_layout(["main_l", "main_r", "sub1", "sub2"],
                             {"sub1": {"level_db": -80.0}, "sub2": {"level_db": 2.9}})
    assert layout["sub1"].get("gain_db") == -80.0, layout["sub1"]
    assert layout["sub2"].get("gain_db") == 2.9, layout["sub2"]
    print("2.2 sub mute -80 dB persists backend+runtime, frontend keeps floor: ok")


def _check_convolver_invalidation() -> None:
    text = (ROOT / "static" / "measurement_convolver_editor.js").read_text()
    assert "const previousIrLength = String(conv.irLength" in text
    assert "Convolver taps changed. Take L/R again." in text
    # Taps check must run after the legacy quality remap (which sets irLength
    # without passing through the irLength branch).
    taps_pos = text.index("Convolver taps changed. Take L/R again.")
    quality_pos = text.index("if (field === 'quality') {")
    assert quality_pos < taps_pos, "taps check must cover the quality remap path"
    print("convolver irLength invalidates stale draft: ok")


def _check_headroom_range() -> None:
    app = (ROOT / "static" / "output_effects_ui.js").read_text()
    # -1..-9 are offered when headroom is enabled ("no headroom" is the
    # checkbox's job, so 0 dB is not offered). 0 stays accepted in the
    # allow-list so a previously stored 0 round-trips through
    # render/collect instead of being silently rewritten to -3 dB.
    assert "new Set([-9, -8, -7, -6, -5, -4, -3, -2, -1, 0])" in app, \
        "headroom allow-list must accept -9..0 (0 for legacy round-trip only)"
    html = (ROOT / "static" / "index.html").read_text()
    headroom_start = html.index('id="effects-headroom-gain-db"')
    headroom_block = html[headroom_start:html.index("</select>", headroom_start)]
    for value in ("-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
        assert f'<option value="{value}"' in headroom_block, f"headroom option {value} missing"
    assert '<option value="0"' not in headroom_block, \
        "0 dB must not be offered (checkbox covers no-headroom)"
    print("headroom UI offers -1..-9 dB, stored 0 still round-trips: ok")


def main() -> None:
    _check_sub_mute_minus80()
    _check_convolver_invalidation()
    _check_headroom_range()
    print("ui state sweep regression tests: ok")


if __name__ == "__main__":
    main()
