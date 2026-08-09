#!/usr/bin/env python3

import asyncio
import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import spl_calibration
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
        self.runtime = {
            ("autogain", 0, "bypass"): "false",
            ("autogain", 0, "target"): "-12.0",
            ("loudness", 0, "bypass"): "false",
            ("loudness", 0, "volume"): "-39.5",
            ("loudness", 0, "outputGain"): "27.5",
        }
        self.runtime_events = []

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

    def apply_autogain_loudness_runtime(
        self, previous, extras, *, persist_all_presets=True
    ):
        assert persist_all_presets is False
        return self.apply_global_extras_to_active_preset(extras)

    def get_active_preset(self):
        return ""

    def get_active_plugin_property(self, plugin_name, instance_id, property_name):
        return self.runtime[(plugin_name, instance_id, property_name)]

    def set_active_plugin_property(self, plugin_name, instance_id, property_name, value):
        self.runtime_events.append((plugin_name, instance_id, property_name, value))
        if isinstance(value, bool):
            stored = "true" if value else "false"
        else:
            stored = str(float(value))
        self.runtime[(plugin_name, instance_id, property_name)] = stored


class FakeNoiseProcess:
    def __init__(self, *_args, **_kwargs):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


def test_spl_noise_uses_and_restores_neutral_autogain_loudness_runtime() -> None:
    fake = FakeEasyEffects()
    original_manager = main.easyeffects_manager
    original_get_volume = main.get_output_volume
    original_set_volume = main.set_output_volume
    original_popen = main.subprocess.Popen
    original_gettempdir = main.tempfile.gettempdir
    original_sleep = main.time.sleep
    volume_events = []
    current_volume = [37]

    def set_volume(value):
        current_volume[0] = value
        volume_events.append(value)
    with tempfile.TemporaryDirectory(prefix="fxroute-spl-neutral-") as directory:
        noise_path = Path(directory) / "fxroute-spl-calibration-pink-noise-v2.wav"
        noise_path.write_bytes(b"test")
        try:
            main.easyeffects_manager = fake
            main.get_output_volume = lambda: current_volume[0]
            main.set_output_volume = set_volume
            main.subprocess.Popen = FakeNoiseProcess
            main.tempfile.gettempdir = lambda: directory
            main.time.sleep = lambda _seconds: None
            operation = spl_calibration._SplCalibrationOperation(
                id="noise-test",
                kind="manual-noise",
                session_job_id="spl-calibration:noise-test",
            )

            result = spl_calibration._start_spl_calibration_noise(operation)
            assert result["status"] == "playing"
            assert fake.runtime_events[:3] == [
                ("autogain", 0, "bypass", True),
                ("loudness", 0, "bypass", True),
                ("loudness", 0, "outputGain", 0.0),
            ]
            assert fake.runtime[("autogain", 0, "target")] == "-12.0"
            assert volume_events == [100]
            assert fake.active_apply_calls == 0
            assert fake.all_apply_calls == 0

            spl_calibration._terminate_and_reap(operation.noise_process)
            spl_calibration._restore_spl_calibration_audio(operation)
            assert fake.runtime_events[3:] == [
                ("autogain", 0, "bypass", False),
                ("loudness", 0, "outputGain", 27.5),
                ("loudness", 0, "bypass", False),
            ]
            assert volume_events == [100, 37]
            assert operation.restore_state is None
        finally:
            main.easyeffects_manager = original_manager
            main.get_output_volume = original_get_volume
            main.set_output_volume = original_set_volume
            main.subprocess.Popen = original_popen
            main.tempfile.gettempdir = original_gettempdir
            main.time.sleep = original_sleep


def test_spl_neutralization_failure_restores_runtime() -> None:
    fake = FakeEasyEffects()
    original_manager = main.easyeffects_manager
    original_get_volume = main.get_output_volume
    original_set_volume = main.set_output_volume
    original_sleep = main.time.sleep
    volume_events = []
    original_set_property = fake.set_active_plugin_property
    failed = False

    def fail_neutral_output_gain(plugin_name, instance_id, property_name, value):
        nonlocal failed
        if property_name == "outputGain" and float(value) == 0.0 and not failed:
            failed = True
            raise RuntimeError("neutral output gain failed")
        original_set_property(plugin_name, instance_id, property_name, value)

    try:
        fake.set_active_plugin_property = fail_neutral_output_gain
        main.easyeffects_manager = fake
        main.get_output_volume = lambda: 37
        main.set_output_volume = volume_events.append
        main.time.sleep = lambda _seconds: None
        operation = spl_calibration._SplCalibrationOperation(
            id="failure-test",
            kind="manual-noise",
            session_job_id="spl-calibration:failure-test",
        )
        try:
            spl_calibration._start_spl_calibration_noise(operation)
        except RuntimeError as exc:
            assert str(exc) == "neutral output gain failed"
        else:
            raise AssertionError("neutralization failure was not propagated")
        assert fake.runtime[("loudness", 0, "bypass")] == "false"
        assert fake.runtime[("loudness", 0, "volume")] == "-39.5"
        assert fake.runtime[("loudness", 0, "outputGain")] == "27.5"
        assert fake.runtime[("autogain", 0, "bypass")] == "false"
        assert fake.runtime[("autogain", 0, "target")] == "-12.0"
        assert volume_events == []
        assert operation.restore_state is None
    finally:
        main.easyeffects_manager = original_manager
        main.get_output_volume = original_get_volume
        main.set_output_volume = original_set_volume
        main.time.sleep = original_sleep


async def test_save_applies_only_coupled_loudness_offset() -> None:
    fake = FakeEasyEffects()
    original_manager = main.easyeffects_manager
    original_profile = spl_calibration._spl_output_profile
    original_capability = spl_calibration._spl_auto_capability
    original_set_volume = main.set_output_volume
    system_volume_writes = []
    try:
        main.easyeffects_manager = fake
        spl_calibration._spl_output_profile = lambda: {"id": "usb-a", "label": "USB A"}
        spl_calibration._spl_auto_capability = lambda: {
            "available": True,
            "microphone_model": "UMIK-1",
            "calibration_file_id": "cal-7148364",
            "calibration_filename": "7148364.txt",
        }
        main.set_output_volume = system_volume_writes.append
        spl_calibration._spl_operation = None

        output_before = fake.normalizer._apply_extras_to_output(
            {"plugins_order": []},
            fake.extras,
        )
        result = await spl_calibration.apply_spl_calibration(FakeRequest())
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
        spl_calibration._spl_output_profile = original_profile
        spl_calibration._spl_auto_capability = original_capability
        main.set_output_volume = original_set_volume
        spl_calibration._spl_operation = None


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
    assert round(spl_calibration._calculate_spl_required_adjustment(55.1), 1) == 27.9
    assert spl_calibration._calculate_spl_required_adjustment(83.0) == 0.0
    test_spl_noise_uses_and_restores_neutral_autogain_loudness_runtime()
    test_spl_neutralization_failure_restores_runtime()
    await test_save_applies_only_coupled_loudness_offset()
    test_legacy_trim_is_not_migrated_or_rewritten()
    print("SPL offset is coupled to Loudness only; global gain remains zero: ok")


if __name__ == "__main__":
    asyncio.run(main_test())
