#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SPL automatic capture across pw-record generations.

`--sample-count` only exists on newer pw-record builds (>= 1.6 series;
absent up to 1.4.x, e.g. Debian trixie stock as shipped on ARM boards).
The SPL recorder must therefore:

* pass --sample-count only when the host pw-record advertises it through
  the established supports_option probe (sweep host-capture precedent),
* record unbounded and stop the recorder after the capture window on old
  builds so the WAV is still finalized,
* never start the calibration noise when the recorder is already dead
  (old behavior played noise against a failed capture and only reported
  the recorder error afterwards).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.spl_calibration as spl_calibration
from measurement.spl_calibration import _SplCalibrationOperation


CAL_ID = "680d1373e3-7148364"


def umik_input(**updates):
    result = {
        "id": "pw-source-199",
        "node_name": "alsa_input.usb-miniDSP_Umik-1_Gain__18dB_000-0000-00.analog-stereo",
        "node_description": "Umik-1 Gain 18dB Analog Stereo",
        "device_name": "alsa_card.usb-miniDSP_Umik-1_Gain__18dB_000-0000-00",
        "device_description": "Umik-1 Gain: 18dB",
        "device_vendor_id": "0x2752",
        "device_product_id": "0x0007",
        "device_serial": "miniDSP_Umik-1_Gain:_18dB_000-0000",
        "alsa_card": "3",
        "alsa_device": "0",
        "capture_volume_percent": 100.0,
        "capture_gain_db": 0.0,
    }
    result.update(updates)
    return result


class FakeMeasurementStore:
    def __init__(self, calibration_path, supports_sample_count=True):
        self.calibration_path = calibration_path
        self._supports_sample_count = supports_sample_count

    def get_calibration_state(self):
        filename = self.calibration_path.name.split("-", 1)[-1]
        return {
            "active_calibration_file_id": self.calibration_path.name,
            "calibrations": [{
                "id": self.calibration_path.name,
                "filename": filename,
                "path": str(self.calibration_path),
            }],
        }

    def list_inputs(self):
        return {"inputs": [umik_input()]}

    def _parse_calibration_file(self, _path):
        import numpy as np
        return (
            np.array([10.0, 1000.0, 20000.0]),
            np.array([0.0, 0.0, 0.0]),
        )

    def _pw_record_supports_option(self, option):
        assert option == "--sample-count"
        return self._supports_sample_count


class FakeDependencies:
    def __init__(self, store):
        self._store = store

    def require_dsp_manager(self):
        raise AssertionError("noise start is stubbed in these tests")

    def get_dsp_manager(self):
        return None

    def get_output_volume(self):
        return 50

    def set_output_volume(self, _percent):
        pass

    def get_measurement_store(self):
        return self._store

    def get_measurement_session(self):
        return None

    def read_measurement_settings(self):
        return {"selectedInputId": "pw-source-199", "selectedMicInputChannel": "1"}

    def measurement_entry_preflight(self, rate):
        pass

    def run_dsp_mutation(self, func):
        return func()


class FakeRunResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def pactl_result(command, **_kwargs):
    if command[:2] == ["pactl", "get-source-volume"]:
        return FakeRunResult(stdout="Volume: front-left: 65536 / 100% / 0.00 dB\n")
    if command[:2] == ["pactl", "set-source-volume"]:
        return FakeRunResult()
    if command[:2] == ["pw-link", "-d"]:
        return FakeRunResult()
    raise AssertionError(f"unexpected command: {command}")


class DeadRecorder:
    """pw-record that rejects its CLI at startup (old build behavior)."""

    def __init__(self, argv):
        self.argv = argv
        self.returncode = 1
        self.terminated = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return "", "pw-record: unrecognized option '--sample-count'\n"

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode


class LiveRecorder:
    """Recorder that stays up until terminated, then finalizes its WAV."""

    def __init__(self, argv):
        self.argv = argv
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return "", ""

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def write_sine_wav(path, seconds=6.0, rms=0.05):
    sample_rate = 48000
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    mono = (rms * np.sqrt(2.0) * np.sin(2.0 * np.pi * 1000.0 * t) * 32767).astype(np.int16)
    stereo = np.stack([mono, mono], axis=1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(stereo.tobytes())


def configure(store):
    deps = FakeDependencies(store)
    spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
        get_measurement_store=deps.get_measurement_store,
        get_measurement_session=deps.get_measurement_session,
        get_dsp_manager=deps.get_dsp_manager,
        require_dsp_manager=deps.require_dsp_manager,
        get_output_volume=deps.get_output_volume,
        set_output_volume=deps.set_output_volume,
        read_measurement_settings=deps.read_measurement_settings,
        measurement_entry_preflight=deps.measurement_entry_preflight,
        run_dsp_mutation=deps.run_dsp_mutation,
    ))
    spl_calibration._runtime.operation = None


def calibration_file(directory):
    cal = Path(directory) / f"680d1373e3-7148364.txt"
    cal.write_text(
        '"Sens Factor =0.371dB, SERNO: 7148364"\n10 0\n1000 0\n20000 0\n',
        encoding="utf-8",
    )
    return cal


def main_test():
    with tempfile.TemporaryDirectory() as directory:
        cal = calibration_file(directory)

        # --sample-count command shape, both variants ---------------------
        full = spl_calibration._spl_pw_record_command("some-node", Path("/tmp/x.wav"), use_sample_count=True)
        assert full[:1] == ["pw-record"], full
        assert "--sample-count" in full and full[full.index("--sample-count") + 1] == "240000", full
        assert full[-1] == "/tmp/x.wav", full
        legacy = spl_calibration._spl_pw_record_command("some-node", Path("/tmp/x.wav"), use_sample_count=False)
        assert "--sample-count" not in legacy, legacy
        assert legacy[-1] == "/tmp/x.wav", legacy
        assert legacy[:7] == full[:7], (legacy, full)
        print("recorder command variants: ok")

        # Dead recorder: noise must never start --------------------------
        configure(FakeMeasurementStore(cal, supports_sample_count=False))
        spawned = {}

        def popen_dead(argv, **_kwargs):
            proc = DeadRecorder(argv)
            spawned["proc"] = proc
            return proc

        noise_started = []
        with mock.patch("tempfile.gettempdir", return_value=directory), \
             mock.patch.object(spl_calibration.subprocess, "Popen", side_effect=popen_dead), \
             mock.patch.object(spl_calibration.subprocess, "run", side_effect=pactl_result), \
             mock.patch.object(spl_calibration, "_start_spl_calibration_noise",
                               side_effect=lambda op: noise_started.append(True)):
            try:
                asyncio.run(spl_calibration.measure_spl_automatically())
            except Exception as exc:
                detail = getattr(getattr(exc, "detail", exc), "__str__", lambda: "")()
                assert "unrecognized option" in str(getattr(exc, "detail", exc)), exc
            else:
                raise AssertionError("dead recorder must fail the automatic run")
        assert noise_started == [], "calibration noise started against a dead recorder"
        assert "--sample-count" not in spawned["proc"].argv, spawned["proc"].argv
        print("dead recorder never starts noise: ok")

        # Legacy build: unbounded record stopped after the window ---------
        configure(FakeMeasurementStore(cal, supports_sample_count=False))
        spawned.clear()
        slept = []

        def popen_live(argv, **_kwargs):
            proc = LiveRecorder(argv)
            spawned["proc"] = proc
            write_sine_wav(Path(argv[-1]))
            return proc

        with mock.patch("tempfile.gettempdir", return_value=directory), \
             mock.patch.object(spl_calibration.subprocess, "Popen", side_effect=popen_live), \
             mock.patch.object(spl_calibration.subprocess, "run", side_effect=pactl_result), \
             mock.patch.object(spl_calibration, "_start_spl_calibration_noise",
                               return_value={"status": "playing"}), \
             mock.patch.object(spl_calibration.time, "sleep",
                               side_effect=lambda s: slept.append(s)):
            result = asyncio.run(spl_calibration.measure_spl_automatically())
        assert "--sample-count" not in spawned["proc"].argv, spawned["proc"].argv
        assert spawned["proc"].terminated is True, "fallback must terminate the recorder"
        assert slept and 4.5 <= slept[0] <= 6.0, slept
        assert result["status"] == "ok", result
        assert 40.0 <= result["measured_spl_db"] <= 130.0, result
        print("legacy fallback terminates after the window: ok")

        # Modern build: sample-count path unchanged ----------------------
        configure(FakeMeasurementStore(cal, supports_sample_count=True))
        spawned.clear()

        def popen_count(argv, **_kwargs):
            proc = LiveRecorder(argv)
            spawned["proc"] = proc
            write_sine_wav(Path(argv[-1]))
            return proc

        with mock.patch("tempfile.gettempdir", return_value=directory), \
             mock.patch.object(spl_calibration.subprocess, "Popen", side_effect=popen_count), \
             mock.patch.object(spl_calibration.subprocess, "run", side_effect=pactl_result), \
             mock.patch.object(spl_calibration, "_start_spl_calibration_noise",
                               return_value={"status": "playing"}):
            result = asyncio.run(spl_calibration.measure_spl_automatically())
        assert spawned["proc"].argv[spawned["proc"].argv.index("--sample-count") + 1] == "240000"
        assert spawned["proc"].terminated is False, "sample-count path must not terminate"
        assert result["status"] == "ok", result
        print("sample-count path unchanged: ok")

    print("SPL pw-record capture variants: ok")


if __name__ == "__main__":
    main_test()
