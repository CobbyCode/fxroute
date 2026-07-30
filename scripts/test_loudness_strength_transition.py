#!/usr/bin/env python3

import copy
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easyeffects import EasyEffectsManager
import main as fxroute_main


def make_extras(manager, strength):
    return manager.normalize_effects_extras({
        "loudness": {
            "enabled": True,
            "params": {
                "fftSize": 4096,
                "strength": strength,
                "volumeDb": -21.39284,
                "calibration": {"requiredAdjustmentDb": 17.6},
                "calibrationProfiles": {},
            },
        },
    })


def run_transition(start, target):
    root = Path(tempfile.mkdtemp(prefix="fxroute-strength-transition-"))
    manager = EasyEffectsManager(home=root)
    manager.output_dir = root / "output"
    manager.output_dir.mkdir(parents=True)
    manager.global_extras_file = root / "extras.json"

    previous = make_extras(manager, start)
    current = make_extras(manager, target)
    base_output = manager._apply_extras_to_output({"plugins_order": []}, previous)
    (manager.output_dir / "Test.json").write_text(
        json.dumps({"output": base_output}, indent=2) + "\n"
    )

    loads = []
    persisted = []
    manager.get_active_preset = lambda: "Test"
    manager.load_preset = lambda name, **kwargs: loads.append(
        (
            name,
            kwargs,
            copy.deepcopy(
                json.loads((manager.output_dir / "Test.json").read_text())
                ["output"]["loudness#0"]
            ),
        )
    )
    original_save = manager.save_global_extras

    def save_once(extras):
        persisted.append(copy.deepcopy(extras))
        return original_save(extras)

    manager.save_global_extras = save_once
    result = manager.apply_loudness_strength_transition(previous, current)

    assert len(persisted) == 1
    assert len(loads) == 2
    assert all(load[1] == {"sync_global_extras": False} for load in loads)
    assert all(load[2]["bypass"] is False for load in loads)

    old_plugin = manager._loudness_plugin_payload(previous["loudness"])
    final_plugin = manager._loudness_plugin_payload(current["loudness"])
    intermediate = loads[0][2]
    final = loads[1][2]
    old_sum = old_plugin["volume"] + old_plugin["output-gain"]
    intermediate_sum = intermediate["volume"] + intermediate["output-gain"]
    final_sum = final["volume"] + final["output-gain"]
    assert intermediate_sum <= old_sum
    assert math.isclose(final_sum, old_sum, abs_tol=1e-9)
    assert final["volume"] == final_plugin["volume"]
    assert final["output-gain"] == final_plugin["output-gain"]
    assert result["extras"]["loudness"]["params"]["strength"] == target
    return intermediate, old_sum


def main():
    strengths = ("full", "med", "light", "min")
    for left, right in zip(strengths, strengths[1:]):
        increasing, increasing_sum = run_transition(left, right)
        decreasing, decreasing_sum = run_transition(right, left)
        assert math.isclose(increasing["volume"], -38.99284 + 10.0 * strengths.index(left), abs_tol=1e-9)
        assert math.isclose(
            increasing["volume"] + increasing["output-gain"],
            increasing_sum - 10.0,
            abs_tol=1e-9,
        )
        assert math.isclose(
            decreasing["volume"] + decreasing["output-gain"],
            decreasing_sum - 10.0,
            abs_tol=1e-9,
        )
    baseline = make_extras(EasyEffectsManager(home=Path(tempfile.mkdtemp())), "full")
    strength_only = copy.deepcopy(baseline)
    strength_only["loudness"]["params"]["strength"] = "med"
    strength_and_fft = copy.deepcopy(strength_only)
    strength_and_fft["loudness"]["params"]["fftSize"] = 8192
    assert fxroute_main._is_pure_loudness_strength_change(baseline, strength_only)
    assert not fxroute_main._is_pure_loudness_strength_change(baseline, strength_and_fft)
    print("Loudness Strength transitions: safe order, no boost, no bypass, one persistence write: ok")


if __name__ == "__main__":
    main()
