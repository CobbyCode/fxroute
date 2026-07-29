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
                    "volumeDb": -20.23453,
                    "calibration": {"requiredAdjustmentDb": 22.9},
                },
            },
        },
    )
    assert disabled["loudness#0"]["bypass"] is True
    assert disabled["loudness#0"]["volume"] == -20.23453
    assert disabled["loudness#0"]["output-gain"] == 0.0


def test_mutex_and_fft_validation() -> None:
    ee = manager()
    try:
        ee.normalize_effects_extras({
            "autogain": {"enabled": True},
            "loudness": {"enabled": True},
        })
    except ValueError as exc:
        assert "cannot be enabled" in str(exc)
    else:
        raise AssertionError("backend mutex was not enforced")
    for fft_size in (256, 512, 1024, 2048, 4096, 8192, 16384):
        normalized = ee.normalize_effects_extras({
            "loudness": {"enabled": True, "params": {"fftSize": fft_size}},
        })
        assert normalized["loudness"]["params"]["fftSize"] == fft_size


def test_persistence_and_volume_mapping() -> None:
    ee = manager()
    saved = ee.save_global_extras({
        "loudness": {
            "enabled": True,
            "params": {
                "fftSize": 4096,
                "volumeDb": ee.loudness_db_from_percent(62),
                "calibration": {"outputProfileId": "usb-a", "requiredAdjustmentDb": -2.5},
                "calibrationProfiles": {"usb-a": {"requiredAdjustmentDb": -2.5}},
            },
        },
    })
    loaded = ee.load_global_extras()
    assert loaded == saved
    assert loaded["loudness"]["params"]["calibrationProfiles"]["usb-a"]["requiredAdjustmentDb"] == -2.5
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
    test_mutex_and_fft_validation()
    test_persistence_and_volume_mapping()
    print(json.dumps({"status": "ok", "tests": 3}))


if __name__ == "__main__":
    main()
