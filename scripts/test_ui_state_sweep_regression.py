#!/usr/bin/env python3
"""Regression: UI-state sweep findings (pre-beta audit).

  1. 2.2 sub level -80 dB round-trip: AutoSub mutes inactive 2.2 subs to
     -80 (backend/runtime accept -80..12). The frontend clamp (-24) showed
     -24 and re-saved -24 (+56 dB unmute). Frontend now keeps -80.
  2. Convolver draft invalidation: changing sampleRate or irLength (incl.
     the legacy quality path) after Take L/R left a stale draft; Create
     silently used the old rate/taps. Both changes now invalidate the draft.
  3. Headroom range: backend accepts whole dB -9..0, UI offered only -1..-6,
     so stored 0/-7/-8/-9 rendered as -3 and re-saved as -3. UI now spans
     the backend contract.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.samplerate import persistence as persistence
from dsp.runtime import DSPRuntimeConfig

ROOT = Path(__file__).resolve().parents[1]


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


def _check_sub_mute_minus80() -> None:
    text = (ROOT / "static" / "app.js").read_text()
    assert "Math.max(-80, Math.min(12, Number(input.level_db" in text, \
        "normalizeSingleSubwooferSettings must keep the -80 mute floor"
    assert "Math.max(-80, Math.min(12, Number(input.sub_level_db" in text, \
        "normalizeSubwooferSettings must keep the -80 mute floor for the 2.2 global display"

    def run() -> None:
        path = persistence._audio_output_mode_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        sub = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
               "sub_level_db": 0.0, "sub_alignment_ms": 0.0, "sub_polarity": "normal"}
        subs = {"sub1": {"level_db": -80.0, "alignment_ms": 0.0, "polarity": "normal"},
                "sub2": {"level_db": 2.9, "alignment_ms": -0.2, "polarity": "normal"}}
        built = persistence._build_audio_output_mode_payload("subwoofer-2.2", dict(sub), dict(subs))
        path.write_text(json.dumps(built, indent=2) + "\n")
        loaded = persistence._load_audio_output_mode()
        assert loaded["subwoofers"]["sub1"]["level_db"] == -80.0, loaded
        ov = {"output_mode": loaded, "selected_output": {"key": "k"}, "current_output": {"key": "k"}}
        rt = DSPRuntimeConfig.from_overview(ov)
        by_name = {c["name"]: c for c in rt.layout}
        assert by_name["SUB1"].get("gain_db") == -80.0, by_name["SUB1"]
        assert by_name["SUB2"].get("gain_db") == 2.9, by_name["SUB2"]

    _with_temp_home(run)
    print("2.2 sub mute -80 dB persists backend+runtime, frontend keeps floor: ok")


def _check_convolver_invalidation() -> None:
    text = (ROOT / "static" / "app.js").read_text()
    assert "const previousIrLength = String(conv.irLength" in text
    assert "const previousSampleRate = String(state.measurement?.measurementSampleRate" in text
    assert "Sample rate changed. Take L/R again." in text
    assert "Convolver taps changed. Take L/R again." in text
    # Taps check must run after the legacy quality remap (which sets irLength
    # without passing through the irLength branch).
    taps_pos = text.index("Convolver taps changed. Take L/R again.")
    quality_pos = text.index("if (field === 'quality') {")
    assert quality_pos < taps_pos, "taps check must cover the quality remap path"
    print("convolver sampleRate/irLength invalidate stale draft: ok")


def _check_headroom_range() -> None:
    app = (ROOT / "static" / "app.js").read_text()
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
