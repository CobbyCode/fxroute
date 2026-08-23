#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Native LV2 control mappings verified against the installed lsp-plugins
# metadata (sc_limiter_stereo.ttl / loud_comp_stereo.ttl on .104):
#
# Limiter: mode 0=Herm Thin, boost 1=gain-boost enabled (plugin default),
# at/rt/lk clamps match the plugin port maxima (20/20/20 ms), th is linear
# 10^(db/20), slink 0..100.
#
# Loudness: std 4=ISO226-2023 (plugin default), fft 0..6 = 256..16384,
# volume in the -83..+7 dB port range with the total attenuation invariant
# preserved via output-gain.

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager


def compile_limiter(tmp_path, params=None, enabled=True):
    manager = DSPManager(home=tmp_path / "home")
    manager.save_global_extras({
        "limiter": {"enabled": enabled, "params": params or {}},
        "autogain": {"enabled": False}, "loudness": {"enabled": False},
    })
    text = manager.compile_engine_text([
        {"name": "FL", "source": 0}, {"name": "FR", "source": 1}])
    return text


def compile_loudness(tmp_path, params=None, autogain=None):
    manager = DSPManager(home=tmp_path / "home")
    manager.save_global_extras({
        "limiter": {"enabled": False},
        "autogain": autogain or {"enabled": False},
        "loudness": {"enabled": True, "params": params or {}},
    })
    text = manager.compile_engine_text([
        {"name": "FL", "source": 0}, {"name": "FR", "source": 1}])
    return text


def control_lines(text, symbol):
    prefix = f"control {symbol} "
    return [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]


def test_limiter_gain_boost_is_enabled_like_the_old_default(tmp_path):
    text = compile_limiter(tmp_path)
    assert control_lines(text, "boost") == ["1"]


def test_limiter_mode_maps_to_herm_thin(tmp_path):
    text = compile_limiter(tmp_path)
    assert control_lines(text, "mode") == ["0"]


def test_limiter_attack_release_lookahead_clamps_match_plugin_port_bounds(tmp_path):
    text = compile_limiter(tmp_path, {
        "attackMs": 5.0, "releaseMs": 50.0, "lookaheadMs": 10.0})
    assert control_lines(text, "at") == ["5"]
    assert control_lines(text, "rt") == ["20"]
    assert control_lines(text, "lk") == ["10"]
    text = compile_limiter(tmp_path, {
        "attackMs": 0.1, "releaseMs": 1.0, "lookaheadMs": 0.0})
    assert control_lines(text, "at") == ["0.25"]
    assert control_lines(text, "rt") == ["1"]
    assert control_lines(text, "lk") == ["0.1"]


def test_limiter_threshold_is_linear_ratio_and_link_is_passthrough(tmp_path):
    text = compile_limiter(tmp_path, {"thresholdDb": -6.0, "stereoLinkPercent": 60.0})
    assert abs(float(control_lines(text, "th")[0]) - 10.0 ** (-6.0 / 20.0)) < 1e-8
    assert control_lines(text, "slink") == ["60"]


def test_limiter_sidechain_and_output_controls_match_old_payload(tmp_path):
    text = compile_limiter(tmp_path)
    assert control_lines(text, "scp") == ["1"]
    assert control_lines(text, "alr") == ["0"]
    assert control_lines(text, "alr_at") == ["5"]
    assert control_lines(text, "alr_rt") == ["50"]
    assert control_lines(text, "knee") == ["1"]
    assert control_lines(text, "smooth") == []
    assert control_lines(text, "ovs") == ["0"]
    assert control_lines(text, "dith") == ["0"]
    assert control_lines(text, "extsc") == ["0"]
    assert control_lines(text, "g_in") == ["1"]
    assert control_lines(text, "g_out") == ["1"]
    for forbidden in ("smooth", "in2lk", "in2sc", "lk2in", "lk2sc", "sc2in", "sc2lk"):
        assert control_lines(text, forbidden) == []


def test_loudness_enums_match_installed_lsp_metadata(tmp_path):
    text = compile_loudness(tmp_path)
    for forbidden in ("mode", "approx"):
        assert control_lines(text, forbidden) == []
    assert control_lines(text, "std") == ["4"]
    assert control_lines(text, "fft") == ["4"]
    assert control_lines(text, "hclip") == ["0"]
    assert control_lines(text, "hcrange") == ["6"]
    assert control_lines(text, "input") == ["1"]
    text = compile_loudness(tmp_path, {"fftSize": 16384})
    assert control_lines(text, "fft") == ["6"]
    text = compile_loudness(tmp_path, {"fftSize": 256})
    assert control_lines(text, "fft") == ["0"]


def test_loudness_volume_stays_within_lsp_port_range(tmp_path):
    manager = DSPManager(home=tmp_path / "home")
    loudness = manager.normalize_effects_extras({
        "loudness": {"enabled": True, "params": {
            "volumeDb": 0.0, "strength": 1,
            "calibration": {"requiredAdjustmentDb": -50.0}}}})["loudness"]
    payload = manager._loudness_plugin_payload(loudness, manager.AUTOGAIN_DEFAULTS)
    assert payload["volume"] == manager.LOUDNESS_PLUGIN_VOLUME_MAX_DB
    assert abs(payload["volume"] + payload["output-gain"]) < 1e-9


def test_loudness_volume_clamp_moves_remainder_to_output_gain_param(tmp_path):
    text = compile_loudness(tmp_path, {
        "volumeDb": 0.0, "strength": 1,
        "calibration": {"requiredAdjustmentDb": -50.0}})
    param = [line for line in text.splitlines() if line.startswith("param output_gain_db ")]
    volume = [line for line in text.splitlines() if line.startswith("control volume ")]
    assert len(param) == 1 and len(volume) == 1
    output_gain = float(param[0].split()[-1])
    plugin_volume = float(volume[0].split()[-1])
    assert plugin_volume == 7.0
    assert abs(plugin_volume + output_gain) < 1e-9


def test_loudness_work_point_follows_volume_and_stage_is_level_neutral(tmp_path):
    # The LSP work point must keep following the canonical volume
    # (volumeDb - calibration + strength + AutoGain), while the stage stays
    # level-neutral at the pre-master meter tap via the inverse output
    # compensation.  The canonical listening attenuation is applied by the
    # master gain stage right after the tap, before the protection limiter.
    quiet = compile_loudness(tmp_path, {"volumeDb": -37.19, "strength": 7})
    loud = compile_loudness(tmp_path, {"volumeDb": 0.0, "strength": 7})

    def control(text, symbol):
        return float([line for line in text.splitlines()
                      if line.startswith(f"control {symbol} ")][0].split()[-1])

    def param(text, symbol):
        return float([line for line in text.splitlines()
                      if line.startswith(f"param {symbol} ")][0].split()[-1])

    assert control(quiet, "volume") != control(loud, "volume")
    for text in (quiet, loud):
        assert abs(control(text, "volume") + param(text, "output_gain_db")) < 1e-9


def _chain_blocks(text):
    """Split the engine text into stage blocks with their inner lines."""
    blocks = []
    current = None
    for line in text.splitlines():
        if line.startswith("stage_begin "):
            current = {"header": line, "lines": []}
            blocks.append(current)
        elif current is not None and line == "stage_end":
            current = None
        elif current is not None:
            current["lines"].append(line)
    return blocks


def _block_value(block, prefix):
    for line in block["lines"]:
        if line.startswith(prefix):
            return float(line.split()[-1])
    return None


def test_limiter_input_is_level_neutral_pre_master_level(tmp_path):
    # The loudness stage keeps work point p with the inverse host
    # compensation (-p) and the graph master position between the pre-master
    # meter tap and the protection limiter is 0 dB: the stage net is 0 dB, so
    # the Limiter input is the unattenuated pre-master level.  The single
    # global FXRoute master is the system volume applied after the whole DSP
    # chain; Loudness volumeDb is only the ISO-226 work point and never owns
    # a gain stage.
    cases = (
        (0.0, 10, 0.0, -23.0, False),
        (-37.19, 10, 0.0, -23.0, False),
        (-20.0, 7, 2.5, -18.0, True),
        (-80.0, 1, -50.0, -12.0, True),
        (-41.94, 5, 4.25, -15.0, True),
        (-25.9, 2, 0.0, -18.0, True),
    )
    for volume_db, strength, calibration_db, autogain_db, autogain_on in cases:
        manager = DSPManager(home=tmp_path / "home")
        manager.save_global_extras({
            "limiter": {"enabled": True, "params": {}},
            "autogain": {"enabled": autogain_on, "params": {"targetDb": autogain_db}},
            "loudness": {"enabled": True, "params": {
                "fftSize": 4096, "strength": strength, "volumeDb": volume_db,
                "calibration": {"requiredAdjustmentDb": calibration_db},
                "calibrationProfiles": {}}},
        })
        text = manager.compile_engine_text([
            {"name": "FL", "source": 0}, {"name": "FR", "source": 1}])
        blocks = _chain_blocks(text)
        kinds = [block["header"].split()[4] for block in blocks]
        loudness_index = kinds.index("http://lsp-plug.in/plugins/lv2/loud_comp_stereo")
        limiter_index = kinds.index("http://lsp-plug.in/plugins/lv2/sc_limiter_stereo")

        strength_db = (10 - strength) * (30.0 / 9.0)
        autogain_contrib = (autogain_db + 23.0) if autogain_on else 0.0
        p = volume_db - calibration_db + strength_db + autogain_contrib
        p_clamped = max(-83.0, min(7.0, p))

        loudness = blocks[loudness_index]
        assert abs(_block_value(loudness, "control volume ") - p_clamped) < 1e-6
        assert abs(_block_value(loudness, "param output_gain_db ") + p_clamped) < 1e-6

        # The graph master gain sits right after the loudness stage and
        # before the protection limiter.  It is level-neutral (0 dB): the
        # stage net (p - p) plus the 0 dB master equals the unattenuated
        # pre-master Limiter input.
        master = blocks[loudness_index + 1]
        assert master["header"].split()[4] == "master_gain"
        assert abs(_block_value(master, "param gain_db ")) < 1e-6
        assert limiter_index == loudness_index + 2
        assert abs(p_clamped - p_clamped) < 1e-6


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
