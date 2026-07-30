#!/usr/bin/env python3

import copy
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easyeffects import EasyEffectsManager


class RecordingManager(EasyEffectsManager):
    def __init__(self):
        super().__init__(home=Path(tempfile.mkdtemp(prefix="fxroute-strength-runtime-")))
        self.runtime_updates = []
        self.persist_calls = 0
        self.LOUDNESS_STRENGTH_GUARD_SETTLE_SECONDS = 0.0
        self.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0.0
        self.LOUDNESS_STRENGTH_RAMP_INTERVAL_SECONDS = 0.0

    def set_active_plugin_property(self, plugin_name, instance_id, property_name, value):
        self.runtime_updates.append((plugin_name, instance_id, property_name, float(value)))

    def apply_global_extras_to_all_presets(self, extras):
        self.persist_calls += 1
        normalized = self.save_global_extras(extras)
        return {"extras": normalized, "updated": 1, "skipped": []}


def extras(strength):
    return {
        "loudness": {
            "enabled": True,
            "params": {
                "fftSize": 8192,
                "strength": strength,
                "volumeDb": -25.212984202991393,
                "calibration": {"requiredAdjustmentDb": 17.6},
                "calibrationProfiles": {},
            },
        },
    }


def test_all_adjacent_transitions():
    manager = RecordingManager()
    levels = ("full", "med", "light", "min")
    transitions = list(zip(levels, levels[1:])) + list(
        zip(reversed(levels), reversed(levels[:-1]))
    )
    for old, new in transitions:
        manager.runtime_updates.clear()
        manager.persist_calls = 0
        result = manager.apply_loudness_strength_runtime(extras(old), extras(new))
        updates = manager.runtime_updates
        old_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(extras(old))["loudness"]
        )
        new_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(extras(new))["loudness"]
        )
        assert updates[0][2] == "outputGain"
        guard = updates[0][3]
        assert math.isclose(
            guard,
            min(old_payload["output-gain"], new_payload["output-gain"])
            - manager.LOUDNESS_STRENGTH_GUARD_DB,
            abs_tol=1e-9,
        )
        assert guard + old_payload["volume"] < (
            old_payload["output-gain"] + old_payload["volume"]
        )
        assert updates[1][2:] == (
            "volume",
            float(new_payload["volume"]),
        )
        ramp = updates[2:]
        assert all(item[2] == "outputGain" for item in ramp)
        assert all(
            ramp[index][3] <= ramp[index + 1][3]
            for index in range(len(ramp) - 1)
        )
        assert math.isclose(
            ramp[-1][3], new_payload["output-gain"], abs_tol=1e-9
        )
        assert all(
            item[3] + new_payload["volume"]
            <= new_payload["output-gain"] + new_payload["volume"] + 1e-9
            for item in ramp
        )
        assert math.isclose(
            ramp[-1][3] + new_payload["volume"],
            old_payload["volume"] + old_payload["output-gain"],
            abs_tol=1e-9,
        )
        assert manager.persist_calls == 1
        assert result["extras"]["loudness"]["params"]["strength"] == new


def test_rollback_on_second_property_failure():
    manager = RecordingManager()
    calls = []

    def fail_second(plugin_name, instance_id, property_name, value):
        calls.append((property_name, float(value)))
        if len(calls) == 2:
            raise RuntimeError("second property failed")

    manager.set_active_plugin_property = fail_second
    try:
        manager.apply_loudness_strength_runtime(extras("min"), extras("light"))
    except RuntimeError as exc:
        assert str(exc) == "second property failed"
    else:
        raise AssertionError("runtime failure was not propagated")
    assert calls[0][0] == "outputGain"
    assert calls[1][0] == "volume"
    assert calls[2][0] == "outputGain"
    assert calls[3][0] == "volume"
    assert calls[-1][0] == "outputGain"
    assert manager.persist_calls == 0


def main():
    test_all_adjacent_transitions()
    test_rollback_on_second_property_failure()
    print("Loudness direct runtime strength transitions: ok")


if __name__ == "__main__":
    main()
