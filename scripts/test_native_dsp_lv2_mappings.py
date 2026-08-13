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
# Loudness: mode 0=FFT, std 4=ISO226-2023 (plugin default), approx 2=Normal
# (plugin default), fft 0..6 = 256..16384, volume in the -83..+7 dB port
# range with the total attenuation invariant preserved via output-gain.

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager


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
    assert control_lines(text, "smooth") == ["-5"]
    assert control_lines(text, "ovs") == ["0"]
    assert control_lines(text, "dith") == ["0"]
    assert control_lines(text, "extsc") == ["0"]
    assert control_lines(text, "g_in") == ["1"]
    assert control_lines(text, "g_out") == ["1"]


def test_loudness_enums_match_installed_lsp_metadata(tmp_path):
    text = compile_loudness(tmp_path)
    assert control_lines(text, "mode") == ["0"]
    assert control_lines(text, "std") == ["4"]
    assert control_lines(text, "approx") == ["2"]
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


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
