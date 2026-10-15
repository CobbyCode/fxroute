"""Compile v2 layouts for the retained bass-management safety fixtures."""

import hashlib
import json
from audio.output_state import (default_output_state, set_bass_management,
                                set_mode_routing, set_output_processing, switch_mode)
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan


def peak_kwargs(config):
    subs = ("sub_l", "sub_r") if config.bass_routing == "stereo" else ("sub1", "sub2")
    state = switch_mode(default_output_state(), "stereo-sub")
    state = set_mode_routing(state, "stereo-sub", "fixture", ["main_l", "main_r", *subs])
    state = set_bass_management(
        state, "stereo-sub", frequency_hz=config.crossover_frequency_hz,
        main_highpass_enabled=config.main_highpass_enabled)
    for role, level, delay, polarity in (
            (subs[0], config.sub_level_db, config.sub_alignment_ms, config.sub_polarity),
            (subs[1], config.sub2_level_db, config.sub2_alignment_ms, config.sub2_polarity)):
        state = set_output_processing(state, "stereo-sub", role,
                                      level_db=level, alignment_ms=delay, polarity=polarity)
    plan = compile_processing_plan(
        state, output_key="fixture", channels=4, sample_rate_hz=config.sample_rate,
        preset_loader=lambda *args: {"chain": []}, neutralize_banks=True)
    return {"layout": layout_from_plan(plan, resolve_ir=lambda *args: None),
            "plan_fingerprint": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}
