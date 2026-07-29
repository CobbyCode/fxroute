#!/usr/bin/env python3

import asyncio
import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from easyeffects import EasyEffectsManager


class FakeRequest:
    async def json(self):
        return {"measured_spl_db": 55.1}


class FakeEasyEffects:
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    def __init__(self):
        self.normalizer = EasyEffectsManager(
            home=Path(tempfile.mkdtemp(prefix="fxroute-spl-assistant-"))
        )
        self.extras = self.normalizer.normalize_effects_extras({
            "loudness": {
                "enabled": True,
                "params": {
                    "volumeDb": -12.0,
                    "calibration": {"requiredAdjustmentDb": 27.9},
                    "calibrationProfiles": {"usb-a": {"requiredAdjustmentDb": 27.9}},
                },
            },
        })
        self.active_apply_calls = 0
        self.all_apply_calls = 0

    def load_global_extras(self):
        return copy.deepcopy(self.extras)

    def save_global_extras(self, extras):
        self.extras = self.normalizer.normalize_effects_extras(extras)
        return copy.deepcopy(self.extras)

    def apply_global_extras_to_active_preset(self, extras):
        self.active_apply_calls += 1
        self.extras = self.normalizer.normalize_effects_extras(extras)
        return {"extras": copy.deepcopy(self.extras)}

    def apply_global_extras_to_all_presets(self, extras):
        self.all_apply_calls += 1
        raise AssertionError("SPL metadata save must not rewrite DSP presets")

    def get_active_preset(self):
        return ""


async def test_save_applies_only_coupled_loudness_offset() -> None:
    fake = FakeEasyEffects()
    original_manager = main.easyeffects_manager
    original_profile = main._spl_output_profile
    original_capability = main._spl_auto_capability
    original_set_volume = main.set_output_volume
    system_volume_writes = []
    try:
        main.easyeffects_manager = fake
        main._spl_output_profile = lambda: {"id": "usb-a", "label": "USB A"}
        main._spl_auto_capability = lambda: {
            "available": True,
            "microphone_model": "UMIK-1",
            "calibration_file_id": "cal-7148364",
            "calibration_filename": "7148364.txt",
        }
        main.set_output_volume = system_volume_writes.append
        main.spl_calibration_noise_process = None
        main.spl_calibration_restore_state = None

        output_before = fake.normalizer._apply_extras_to_output(
            {"plugins_order": []},
            fake.extras,
        )
        result = await main.apply_spl_calibration(FakeRequest())
        params = fake.extras["loudness"]["params"]
        calibration = params["calibration"]

        assert round(result["required_adjustment_db"], 1) == 27.9
        assert result["calibrated"] is False
        assert calibration["measuredSplDb"] == 55.1
        assert round(calibration["requiredAdjustmentDb"], 1) == 27.9
        assert calibration["calibrated"] is False
        assert params["volumeDb"] == -12.0
        assert fake.active_apply_calls == 1
        assert fake.all_apply_calls == 0
        assert system_volume_writes == []

        output = fake.normalizer._apply_extras_to_output(
            {"plugins_order": []},
            fake.extras,
        )
        assert output["limiter#0"] == output_before["limiter#0"]
        assert output["loudness#0"]["volume"] == output_before["loudness#0"]["volume"]
        assert output["limiter#0"]["input-gain"] == 0.0
        assert output["loudness#0"]["input-gain"] == 0.0
        assert output["loudness#0"]["output-gain"] == 27.9
    finally:
        main.easyeffects_manager = original_manager
        main._spl_output_profile = original_profile
        main._spl_auto_capability = original_capability
        main.set_output_volume = original_set_volume
        main.spl_calibration_restore_state = None


def test_legacy_trim_is_not_migrated_or_rewritten() -> None:
    with tempfile.TemporaryDirectory(prefix="fxroute-spl-migration-") as directory:
        manager = EasyEffectsManager(home=Path(directory))
        manager.global_extras_file.parent.mkdir(parents=True, exist_ok=True)
        manager.global_extras_file.write_text(
            """{
  "loudness": {
    "enabled": true,
    "params": {
      "volumeDb": -6.0,
      "calibrationTrimDb": 27.9,
      "calibration": {"trimDb": 27.9},
      "calibrationProfiles": {"usb-a": {"trimDb": 27.9}}
    }
  }
}
""",
            encoding="utf-8",
        )
        original_text = manager.global_extras_file.read_text()
        loaded = manager.load_global_extras()
        assert "calibrationTrimDb" not in loaded["loudness"]["params"]
        assert "requiredAdjustmentDb" not in loaded["loudness"]["params"]["calibration"]
        assert manager.global_extras_file.read_text() == original_text


async def main_test() -> None:
    assert round(main._calculate_spl_required_adjustment(55.1), 1) == 27.9
    assert main._calculate_spl_required_adjustment(83.0) == 0.0
    await test_save_applies_only_coupled_loudness_offset()
    test_legacy_trim_is_not_migrated_or_rewritten()
    print("SPL offset is coupled to Loudness only; global gain remains zero: ok")


if __name__ == "__main__":
    asyncio.run(main_test())
