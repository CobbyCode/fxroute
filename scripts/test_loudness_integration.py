#!/usr/bin/env python3

import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easyeffects import EasyEffectsManager
from system_volume import volume_db_to_percent, volume_percent_to_db


def manager() -> EasyEffectsManager:
    return EasyEffectsManager(home=Path(tempfile.mkdtemp(prefix="fxroute-loudness-test-")))


def test_chain_and_schema() -> None:
    ee = manager()
    output = ee._apply_extras_to_output(
        {
            "equalizer#0": {"bypass": False, "output-gain": 0.0},
            "plugins_order": ["equalizer#0"],
        },
        {
            "limiter": {"enabled": True},
            "autogain": {"enabled": False},
            "loudness": {
                "enabled": True,
                "params": {
                    "fftSize": 8192,
                    "strength": 10,
                    "volumeDb": -20.23453,
                    "calibration": {"requiredAdjustmentDb": 22.9},
                    "calibrationProfiles": {},
                },
            },
        },
    )
    assert output["plugins_order"][-2:] == ["loudness#0", "limiter#0"]
    loudness = output["loudness#0"]
    assert loudness == {
        "bypass": False,
        "clipping": False,
        "clipping-range": 6.0,
        "fft": "8192",
        "iir-approximation": "Normal",
        "input-gain": 0.0,
        "mode": "FFT",
        "output-gain": 22.9,
        "std": "ISO226-2023",
        "volume": -43.13453,
    }
    assert output["limiter#0"]["input-gain"] == 0.0

    disabled = ee._apply_extras_to_output(
        {"plugins_order": []},
        {
            "loudness": {
                "enabled": False,
                "params": {
                    "fftSize": 4096,
                    "strength": 10,
                    "volumeDb": -20.23453,
                    "calibration": {"requiredAdjustmentDb": 22.9},
                },
            },
        },
    )
    assert disabled["loudness#0"]["bypass"] is True
    assert disabled["loudness#0"]["volume"] == -20.23453
    assert disabled["loudness#0"]["output-gain"] == 0.0


def test_combined_mode_targets_and_fft_validation() -> None:
    ee = manager()
    combined = ee.normalize_effects_extras({
        "autogain": {"enabled": True, "params": {"targetDb": -12}},
        "loudness": {"enabled": True},
    })
    assert combined["autogain"]["enabled"] is True
    assert combined["loudness"]["enabled"] is True
    for target in (-12, -15, -18, -23):
        normalized = ee.normalize_effects_extras({
            "autogain": {"enabled": True, "params": {"targetDb": target}},
        })
        assert normalized["autogain"]["params"]["targetDb"] == target
    for removed in (-9, -14):
        try:
            ee.normalize_effects_extras({
                "autogain": {"enabled": True, "params": {"targetDb": removed}},
            })
        except ValueError as exc:
            assert "must be one of" in str(exc)
        else:
            raise AssertionError(f"unsupported Auto Gain target {removed} was accepted")
    for fft_size in (256, 512, 1024, 2048, 4096, 8192, 16384):
        normalized = ee.normalize_effects_extras({
            "loudness": {"enabled": True, "params": {"fftSize": fft_size}},
        })
        assert normalized["loudness"]["params"]["fftSize"] == fft_size
    for strength in (10, 7, 4, 1):
        normalized = ee.normalize_effects_extras({
            "loudness": {"enabled": True, "params": {"strength": strength}},
        })
        assert normalized["loudness"]["params"]["strength"] == strength


def test_strength_gain_is_neutral() -> None:
    ee = manager()
    expected_offsets = {10: 0.0, 7: 10.0, 4: 20.0, 1: 30.0}
    attenuation = -20.23453
    calibration = 22.9
    for strength, offset in expected_offsets.items():
        output = ee._apply_extras_to_output(
            {"plugins_order": []},
            {
                "loudness": {
                    "enabled": True,
                    "params": {
                        "fftSize": 4096,
                        "strength": strength,
                        "volumeDb": attenuation,
                        "calibration": {"requiredAdjustmentDb": calibration},
                    },
                },
            },
        )
        plugin = output["loudness#0"]
        assert math.isclose(plugin["volume"], attenuation - calibration + offset, abs_tol=1e-9)
        assert math.isclose(plugin["output-gain"], calibration - offset, abs_tol=1e-9)
        assert math.isclose(plugin["volume"] + plugin["output-gain"], attenuation, abs_tol=1e-9)


def test_combined_autogain_offsets_are_neutral() -> None:
    ee = manager()
    attenuation = -26.5
    calibration = 4.25
    strength_offset = 10.0
    for target, autogain_offset in ((-12, 11.0), (-15, 8.0), (-18, 5.0), (-23, 0.0)):
        output = ee._apply_extras_to_output(
            {"plugins_order": []},
            {
                "autogain": {"enabled": True, "params": {"targetDb": target}},
                "loudness": {
                    "enabled": True,
                    "params": {
                        "strength": 7,
                        "volumeDb": attenuation,
                        "calibration": {"requiredAdjustmentDb": calibration},
                    },
                },
            },
        )
        plugin = output["loudness#0"]
        effective_offset = strength_offset + autogain_offset
        assert math.isclose(
            plugin["volume"],
            attenuation - calibration + effective_offset,
            abs_tol=1e-9,
        )
        assert math.isclose(
            plugin["output-gain"],
            calibration - effective_offset,
            abs_tol=1e-9,
        )
        assert math.isclose(
            plugin["volume"] + plugin["output-gain"], attenuation, abs_tol=1e-9
        )
        assert output["plugins_order"][-3:] == [
            "autogain#0", "loudness#0", "limiter#0"
        ]


def test_persistence_and_volume_mapping() -> None:
    ee = manager()
    saved = ee.save_global_extras({
        "autogain": {
            "enabled": True,
            "params": {"targetDb": -23},
        },
        "loudness": {
            "enabled": True,
            "params": {
                "fftSize": 4096,
                "strength": 4,
                "volumeDb": ee.loudness_db_from_percent(62),
                "calibration": {"outputProfileId": "usb-a", "requiredAdjustmentDb": -2.5},
                "calibrationProfiles": {"usb-a": {"requiredAdjustmentDb": -2.5}},
            },
        },
    })
    loaded = ee.load_global_extras()
    assert loaded == saved
    assert loaded["autogain"]["enabled"] is True
    assert loaded["autogain"]["params"]["targetDb"] == -23
    assert loaded["loudness"]["enabled"] is True
    assert loaded["loudness"]["params"]["calibrationProfiles"]["usb-a"]["requiredAdjustmentDb"] == -2.5
    assert loaded["loudness"]["params"]["strength"] == 4
    assert ee.loudness_percent_from_db(loaded["loudness"]["params"]["volumeDb"]) == 62
    assert ee.loudness_db_from_percent(100) == 0.0
    expected = {
        100: 0.0,
        75: 60.0 * math.log10(0.75),
        46: 60.0 * math.log10(0.46),
        25: 60.0 * math.log10(0.25),
        10: -60.0,
        0: -80.0,
    }
    for percent, expected_db in expected.items():
        actual_db = volume_percent_to_db(percent)
        assert math.isclose(actual_db, expected_db, abs_tol=1e-9), (percent, actual_db, expected_db)
        assert ee.loudness_db_from_percent(percent) == actual_db
        assert volume_db_to_percent(actual_db) == percent
    assert math.isclose(volume_percent_to_db(46), -20.23453, abs_tol=0.00001)


def main() -> None:
    test_chain_and_schema()
    test_combined_mode_targets_and_fft_validation()
    test_strength_gain_is_neutral()
    test_combined_autogain_offsets_are_neutral()
    test_persistence_and_volume_mapping()
    print(json.dumps({"status": "ok", "tests": 5}))


if __name__ == "__main__":
    main()
