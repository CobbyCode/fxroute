#!/usr/bin/env python3

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class FakeEasyEffects:
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    def __init__(self):
        self.extras = {
            "loudness": {
                "enabled": True,
                "params": {
                    "volumeDb": 0.0,
                    "calibrationTrimDb": 0.0,
                },
            },
        }
        self.applied = []

    def load_global_extras(self):
        return copy.deepcopy(self.extras)

    def apply_global_extras_to_active_preset(self, extras):
        self.extras = copy.deepcopy(extras)
        self.applied.append(copy.deepcopy(extras))
        return {"extras": self.extras}

    def get_active_preset(self):
        return ""


def main_test() -> None:
    assert main._calculate_spl_calibration_trim(83.0) == 0.0
    assert main._calculate_spl_calibration_trim(80.5) == 2.5
    assert main._spl_auto_capability()["available"] is False

    fake = FakeEasyEffects()
    original_manager = main.easyeffects_manager
    original_set_volume = main.set_output_volume
    restored_volumes = []
    try:
        main.easyeffects_manager = fake
        main.set_output_volume = restored_volumes.append
        main.spl_calibration_noise_process = None
        main.spl_calibration_restore_state = {
            "calibration_trim_db": -2.0,
            "loudness_volume_db": -6.0,
            "system_volume_percent": 62,
        }
        main._stop_spl_calibration_noise()
        params = fake.extras["loudness"]["params"]
        assert params["calibrationTrimDb"] == -2.0
        assert params["volumeDb"] == -6.0
        assert restored_volumes == [62]
        assert main.spl_calibration_restore_state is None
    finally:
        main.easyeffects_manager = original_manager
        main.set_output_volume = original_set_volume
        main.spl_calibration_restore_state = None

    print("SPL trim, manual fallback and calibration-state restoration: ok")


if __name__ == "__main__":
    main_test()
