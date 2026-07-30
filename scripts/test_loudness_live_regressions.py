#!/usr/bin/env python3

import asyncio
import copy
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from easyeffects import EasyEffectsManager
from fastapi import HTTPException


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return copy.deepcopy(self.body)


class FakeBroadcastManager:
    def __init__(self, events):
        self.events = events

    async def broadcast(self, _message):
        self.events.append(("broadcast", _message["type"]))
        return None


class FakeEasyEffects:
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    def __init__(self, extras, events):
        self._normalizer = EasyEffectsManager(
            home=Path(tempfile.mkdtemp(prefix="fxroute-loudness-regression-"))
        )
        self.extras = self._normalizer.normalize_effects_extras(extras)
        self.events = events

    def load_global_extras(self):
        return copy.deepcopy(self.extras)

    def normalize_effects_extras(self, extras):
        return self._normalizer.normalize_effects_extras(extras)

    def loudness_db_from_percent(self, percent):
        return self._normalizer.loudness_db_from_percent(percent)

    def loudness_percent_from_db(self, value):
        return self._normalizer.loudness_percent_from_db(value)

    def apply_global_extras_to_all_presets(self, extras):
        self.extras = copy.deepcopy(extras)
        self.events.append(("apply", copy.deepcopy(extras)))
        return {"extras": copy.deepcopy(extras), "updated": 1, "skipped": []}

    def apply_loudness_strength_runtime(self, previous, extras):
        self.extras = copy.deepcopy(extras)
        self.events.append((
            "runtime-strength",
            previous["loudness"]["params"]["strength"],
            extras["loudness"]["params"]["strength"],
        ))
        return {"extras": copy.deepcopy(extras), "updated": 1, "skipped": []}

    def get_active_preset(self):
        return "Test"

    def load_preset(self, name):
        self.events.append(("load", name))

    def get_status(self):
        return {"global_extras": copy.deepcopy(self.extras)}


BASE_EXTRAS = {
    "autogain": {"enabled": False},
    "loudness": {
        "enabled": False,
        "params": {
            "fftSize": 4096,
            "strength": "full",
            "volumeDb": 0.0,
            "calibration": {"outputProfileId": "usb-a", "requiredAdjustmentDb": 0.5},
            "calibrationProfiles": {"usb-a": {"requiredAdjustmentDb": 0.5}},
        },
    },
}


async def run_route(body, fake, events, system_volume):
    original_manager = main.easyeffects_manager
    original_broadcast_manager = main.manager
    original_get_volume = main.get_output_volume
    original_set_volume = main.set_output_volume
    original_refresh = main.schedule_peak_monitor_refresh_after_effects_change
    try:
        main.easyeffects_manager = fake
        main.manager = FakeBroadcastManager(events)
        main.get_output_volume = lambda: system_volume[0]

        def set_volume(value):
            events.append(("system", value))
            system_volume[0] = value
            return value

        main.set_output_volume = set_volume
        main.schedule_peak_monitor_refresh_after_effects_change = (
            lambda reason: events.append(("refresh", reason))
        )
        return await main.save_easyeffects_extras(FakeRequest(body))
    finally:
        main.easyeffects_manager = original_manager
        main.manager = original_broadcast_manager
        main.get_output_volume = original_get_volume
        main.set_output_volume = original_set_volume
        main.schedule_peak_monitor_refresh_after_effects_change = original_refresh


async def test_safe_toggle_order_and_partial_persistence():
    events = []
    fake = FakeEasyEffects(BASE_EXTRAS, events)
    system_volume = [46]
    await run_route({"loudnessEnabled": True}, fake, events, system_volume)
    applied = events[0][1]["loudness"]["params"]
    assert events[0][0] == "apply"
    assert events[1] == ("load", "Test")
    assert events[2] == ("system", 100)
    assert math.isclose(applied["volumeDb"], -20.23453, abs_tol=0.00001)
    assert applied["calibration"]["outputProfileId"] == "usb-a"
    assert applied["calibrationProfiles"]["usb-a"]["requiredAdjustmentDb"] == 0.5
    output = fake._normalizer._apply_extras_to_output({"plugins_order": []}, fake.extras)
    assert math.isclose(output["loudness#0"]["volume"], -20.73453, abs_tol=0.00001)
    assert output["loudness#0"]["output-gain"] == 0.5

    events.clear()
    await run_route({"loudnessFftSize": 8192, "loudnessStrength": "light"}, fake, events, system_volume)
    assert fake.extras["loudness"]["params"]["fftSize"] == 8192
    assert fake.extras["loudness"]["params"]["strength"] == "light"
    assert fake.extras["loudness"]["params"]["calibration"]["outputProfileId"] == "usb-a"
    output = fake._normalizer._apply_extras_to_output({"plugins_order": []}, fake.extras)
    assert math.isclose(output["loudness#0"]["volume"], -0.73453, abs_tol=0.00001)
    assert output["loudness#0"]["output-gain"] == -19.5
    assert math.isclose(
        output["loudness#0"]["volume"] + output["loudness#0"]["output-gain"],
        -20.23453,
        abs_tol=0.00001,
    )

    events.clear()
    await run_route({"loudnessEnabled": False}, fake, events, system_volume)
    assert events[0] == ("system", 46)
    assert events[1][0] == "apply"
    assert events[2] == ("load", "Test")
    assert fake.extras["loudness"]["params"]["calibrationProfiles"]["usb-a"]["requiredAdjustmentDb"] == 0.5
    output = fake._normalizer._apply_extras_to_output({"plugins_order": []}, fake.extras)
    assert output["loudness#0"]["output-gain"] == 0.0
    assert math.isclose(output["loudness#0"]["volume"], -20.23453, abs_tol=0.00001)


async def test_mutex_is_http_400():
    events = []
    fake = FakeEasyEffects(BASE_EXTRAS, events)
    try:
        await run_route(
            {"autogainEnabled": True, "loudnessEnabled": True},
            fake,
            events,
            [46],
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail["code"] == "invalid_effects_extras"
        assert "cannot be enabled" in exc.detail["message"]
    else:
        raise AssertionError("mutex request did not fail")


async def test_pure_strength_uses_runtime_path_once():
    events = []
    enabled = copy.deepcopy(BASE_EXTRAS)
    enabled["loudness"]["enabled"] = True
    enabled["loudness"]["params"]["strength"] = "min"
    fake = FakeEasyEffects(enabled, events)
    await run_route({"loudnessStrength": "light"}, fake, events, [100])
    assert events == [
        ("runtime-strength", "min", "light"),
        ("broadcast", "easyeffects"),
    ]


async def test_strength_ignores_stale_volume_from_ui():
    events = []
    enabled = copy.deepcopy(BASE_EXTRAS)
    enabled["loudness"]["enabled"] = True
    enabled["loudness"]["params"]["strength"] = "min"
    enabled["loudness"]["params"]["volumeDb"] = -42.0
    fake = FakeEasyEffects(enabled, events)
    result = await run_route(
        {
            "loudnessEnabled": True,
            "loudnessStrength": "full",
            "loudnessFftSize": 4096,
            "loudnessVolumeDb": -12.0,
        },
        fake,
        events,
        [100],
    )
    assert events == [
        ("runtime-strength", "min", "full"),
        ("broadcast", "easyeffects"),
    ]
    assert result["extras"]["loudness"]["params"]["volumeDb"] == -42.0
    payload = fake._normalizer._loudness_plugin_payload(
        result["extras"]["loudness"]
    )
    assert math.isclose(
        payload["volume"] + payload["output-gain"], -42.0, abs_tol=1e-9
    )


async def test_duplicate_strength_save_is_noop():
    events = []
    enabled = copy.deepcopy(BASE_EXTRAS)
    enabled["loudness"]["enabled"] = True
    enabled["loudness"]["params"]["strength"] = "light"
    fake = FakeEasyEffects(enabled, events)
    result = await run_route({"loudnessStrength": "light"}, fake, events, [100])
    assert events == []
    assert result["extras"] == fake.extras
    assert result["updated_presets"] == 0
    assert result["skipped_presets"] == []


async def main_test():
    await test_safe_toggle_order_and_partial_persistence()
    await test_mutex_is_http_400()
    await test_pure_strength_uses_runtime_path_once()
    await test_strength_ignores_stale_volume_from_ui()
    await test_duplicate_strength_save_is_noop()
    print("Loudness live regressions: canonical volume, runtime-only strength, duplicate-save no-op, HTTP 400 mutex: ok")


if __name__ == "__main__":
    asyncio.run(main_test())
