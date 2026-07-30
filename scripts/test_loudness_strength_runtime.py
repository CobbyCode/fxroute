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
    for old, new in zip(levels, levels[1:]):
        manager.runtime_updates.clear()
        manager.persist_calls = 0
        result = manager.apply_loudness_strength_runtime(extras(old), extras(new))
        updates = manager.runtime_updates
        assert [item[2] for item in updates] == ["outputGain", "volume"]
        old_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(extras(old))["loudness"]
        )
        new_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(extras(new))["loudness"]
        )
        assert updates[0][3] + old_payload["volume"] <= (
            old_payload["output-gain"] + old_payload["volume"]
        )
        assert math.isclose(
            updates[1][3] + updates[0][3],
            old_payload["output-gain"] + old_payload["volume"],
            abs_tol=1e-9,
        )
        assert manager.persist_calls == 1
        assert result["extras"]["loudness"]["params"]["strength"] == new

    for old, new in zip(reversed(levels), reversed(levels[:-1])):
        manager.runtime_updates.clear()
        manager.persist_calls = 0
        manager.apply_loudness_strength_runtime(extras(old), extras(new))
        updates = manager.runtime_updates
        assert [item[2] for item in updates] == ["volume", "outputGain"]
        old_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(extras(old))["loudness"]
        )
        assert updates[0][3] + old_payload["output-gain"] <= (
            old_payload["volume"] + old_payload["output-gain"]
        )
        assert math.isclose(
            updates[0][3] + updates[1][3],
            old_payload["volume"] + old_payload["output-gain"],
            abs_tol=1e-9,
        )
        assert manager.persist_calls == 1


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
    assert [item[0] for item in calls] == ["volume", "outputGain", "volume"]
    assert manager.persist_calls == 0


def main():
    test_all_adjacent_transitions()
    test_rollback_on_second_property_failure()
    print("Loudness direct runtime strength transitions: ok")


if __name__ == "__main__":
    main()
