#!/usr/bin/env python3
import tempfile
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class FakeMeasurementStore:
    def __init__(self, calibration_path: Path, inputs: list[dict]):
        self.calibration_path = calibration_path
        self.inputs = inputs

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
        return {"inputs": self.inputs}

    def _parse_calibration_file(self, _path):
        return (
            np.array([10.0, 1000.0, 20000.0]),
            np.array([0.0, 0.0, 0.0]),
        )


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


def main_test():
    original_store = main.measurement_store
    original_settings = main._read_measurement_setup_settings
    try:
        with tempfile.TemporaryDirectory() as directory:
            cal = Path(directory) / "680d1373e3-7148364.txt"
            cal.write_text(
                '"Sens Factor =0.371dB, SERNO: 7148364"\n'
                "10 0\n1000 0\n20000 0\n",
                encoding="utf-8",
            )
            main._read_measurement_setup_settings = lambda: {
                "selectedInputId": "pw-source-199",
                "selectedMicInputChannel": "1",
                "selectedReferenceInputChannel": "",
            }
            main.measurement_store = FakeMeasurementStore(cal, [umik_input()])

            header = main._parse_umik_calibration_header(cal)
            assert header == {"sensitivity_factor_db": 0.371, "serial_number": "7148364"}
            assert main._is_umik1_input(umik_input()) is True
            assert main._is_umik1_input(umik_input(device_vendor_id="0x1234")) is False
            assert main._is_umik1_input(umik_input(device_product_id="0x0008")) is False
            assert main._is_umik1_input(
                umik_input(
                    node_name="UMIK-2",
                    node_description="UMIK-2 Gain 18dB",
                    device_name="UMIK-2",
                    device_description="UMIK-2 Gain: 18dB",
                )
            ) is False
            capability = main._spl_auto_capability()
            assert capability["available"] is True
            assert capability["microphone_model"] == "UMIK-1"
            assert capability["serial_number"] == "7148364"
            assert capability["capture_gain_db"] == 0.0
            assert capability["reference_capture_gain_db"] == 0.0
            assert capability["reference_capture_volume_percent"] == 100.0

            wrong_cal = Path(directory) / "680d1373e3-9999999.txt"
            wrong_cal.write_text(
                '"Sens Factor =0.371dB, SERNO: 7148364"\n10 0\n1000 0\n',
                encoding="utf-8",
            )
            main.measurement_store = FakeMeasurementStore(wrong_cal, [umik_input()])
            assert main._spl_auto_capability()["available"] is False

            missing_sensitivity = Path(directory) / "680d1373e3-7148364.txt"
            missing_sensitivity.write_text("SERNO: 7148364\n10 0\n1000 0\n", encoding="utf-8")
            main.measurement_store = FakeMeasurementStore(missing_sensitivity, [umik_input()])
            assert main._spl_auto_capability()["available"] is False

            missing_sensitivity.write_text(
                '"Sens Factor =0.371dB, SERNO: 7148364"\n10 0\n1000 0\n',
                encoding="utf-8",
            )
            main.measurement_store = FakeMeasurementStore(
                missing_sensitivity,
                [umik_input(capture_gain_db=None, capture_volume_percent=None)],
            )
            assert main._spl_auto_capability()["available"] is False

            main.measurement_store = FakeMeasurementStore(
                missing_sensitivity,
                [umik_input(node_description="Umik-1", device_description="Umik-1")],
            )
            capability = main._spl_auto_capability()
            assert capability["available"] is False
            assert capability["checks"]["internal_gain_18_db"] is False

            main.measurement_store = FakeMeasurementStore(missing_sensitivity, [umik_input()])
            sample_rate = 48000
            t = np.arange(sample_rate * 3) / sample_rate
            sine = 0.05 * np.sqrt(2.0) * np.sin(2.0 * np.pi * 1000.0 * t)
            measured = main._c_weighted_spl_from_capture(
                sine, sample_rate, 0.371, missing_sensitivity
            )
            assert abs(measured - (20.0 * np.log10(0.05) + 124.0 - 0.371)) < 0.1

            assert main._calculate_spl_required_adjustment(82.5) == 0.5
    finally:
        main.measurement_store = original_store
        main._read_measurement_setup_settings = original_settings

    print("UMIK-1 metadata, Sens Factor/SERNO, fail-closed gain/cal and manual path: ok")


if __name__ == "__main__":
    main_test()
