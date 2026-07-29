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
    async def broadcast(self, _message):
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
            "volumeDb": 0.0,
            "calibrationTrimDb": 0.5,
            "calibration": {"outputProfileId": "usb-a", "trimDb": 0.5},
            "calibrationProfiles": {"usb-a": {"trimDb": 0.5}},
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
        main.manager = FakeBroadcastManager()
        main.get_output_volume = lambda: system_volume[0]

        def set_volume(value):
            events.append(("system", value))
            system_volume[0] = value
            return value

        main.set_output_volume = set_volume
        main.schedule_peak_monitor_refresh_after_effects_change = lambda _reason: None
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
    assert applied["calibrationTrimDb"] == 0.5
    assert applied["calibration"]["outputProfileId"] == "usb-a"
    assert applied["calibrationProfiles"]["usb-a"]["trimDb"] == 0.5

    events.clear()
    await run_route({"loudnessFftSize": 8192}, fake, events, system_volume)
    assert fake.extras["loudness"]["params"]["fftSize"] == 8192
    assert fake.extras["loudness"]["params"]["calibrationTrimDb"] == 0.5
    assert fake.extras["loudness"]["params"]["calibration"]["outputProfileId"] == "usb-a"

    events.clear()
    await run_route({"loudnessEnabled": False}, fake, events, system_volume)
    assert events[0] == ("system", 46)
    assert events[1][0] == "apply"
    assert events[2] == ("load", "Test")
    assert fake.extras["loudness"]["params"]["calibrationTrimDb"] == 0.5
    assert fake.extras["loudness"]["params"]["calibrationProfiles"]["usb-a"]["trimDb"] == 0.5


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


async def main_test():
    await test_safe_toggle_order_and_partial_persistence()
    await test_mutex_is_http_400()
    print("Loudness live regressions: volume mapping/order, partial persistence, HTTP 400 mutex: ok")


if __name__ == "__main__":
    asyncio.run(main_test())
