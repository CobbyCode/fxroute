#!/usr/bin/env python3
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class FakeMeasurementStore:
    def __init__(self, calibration_path: Path, inputs: list[dict]):
        self.calibration_path = calibration_path
        self.inputs = inputs

    def get_calibration_state(self):
        return {
            "active_calibration_file_id": self.calibration_path.name,
            "calibrations": [{
                "id": self.calibration_path.name,
                "filename": self.calibration_path.name.split("-", 1)[-1],
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


def umik2_input(**updates):
    result = {
        "id": "pw-source-umik2",
        "node_name": "alsa_input.usb-miniDSP_UMIK-2_8107963.analog-stereo",
        "node_description": "miniDSP UMIK-2 Analog Stereo",
        "device_name": "alsa_card.usb-miniDSP_UMIK-2_8107963",
        "device_description": "miniDSP UMIK-2",
        "device_vendor_id": "0x2752",
        "device_product_id": "0x002b",
        "device_serial": "miniDSP_UMIK-2_8107963",
        "alsa_card_name": "UMIK-2",
        "alsa_long_card_name": "miniDSP UMIK-2 at usb-0000:00:14.0-2",
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
            cal = Path(directory) / "cal-8107963.txt"
            valid_header = '"Sens Factor =-12.11dB, AGain =18dB, SERNO: 8107963"\n'
            cal.write_text(valid_header + "10 0\n1000 0\n20000 0\n", encoding="utf-8")
            main._read_measurement_setup_settings = lambda: {
                "selectedInputId": "pw-source-umik2",
                "selectedMicInputChannel": "1",
                "selectedReferenceInputChannel": "",
            }
            main.measurement_store = FakeMeasurementStore(cal, [umik2_input()])

            assert main._UMIK2_PROFILE.parse_calibration_header(cal) == {
                "sensitivity_factor_db": -12.11,
                "analog_gain_db": 18.0,
                "serial_number": "8107963",
            }
            assert main._is_umik2_input(umik2_input()) is True
            assert main._is_umik2_input(umik2_input(device_vendor_id="0x1234")) is False
            assert main._is_umik2_input(umik2_input(device_product_id="0x0007")) is False
            assert main._is_umik2_input(umik2_input(node_description="USB Mic", device_description="USB Mic")) is True
            no_model = umik2_input(
                node_name="generic", node_description="generic", device_name="generic",
                device_description="generic", alsa_card_name="generic",
                alsa_long_card_name="generic",
            )
            assert main._is_umik2_input(no_model) is False

            capability = main._spl_auto_capability()
            assert capability["available"] is True
            assert capability["microphone_model"] == "UMIK-2"
            assert capability["sensitivity_factor_db"] == -12.11
            assert capability["analog_gain_db"] == 18.0
            assert capability["checks"]["capture_reference_state"] is True

            cases = [
                ('"Sens Factor =-12.11dB, SERNO: 8107963"\n', "AGain"),
                ('"Sens Factor =-12.11dB, AGain =12dB, SERNO: 8107963"\n', "factory reference"),
                ('"AGain =18dB, SERNO: 8107963"\n', "Sens Factor"),
                ('"Sens Factor =-12.11dB, AGain =18dB"\n', "SERNO"),
            ]
            for header, expected_reason in cases:
                cal.write_text(header + "10 0\n1000 0\n", encoding="utf-8")
                capability = main._spl_auto_capability()
                assert capability["available"] is False
                assert expected_reason in capability["reason"]

            cal.write_text(valid_header + "10 0\n1000 0\n", encoding="utf-8")
            main.measurement_store = FakeMeasurementStore(cal, [umik2_input(capture_gain_db=-6.0)])
            capability = main._spl_auto_capability()
            assert capability["available"] is False
            assert "100% / 0 dB reference" in capability["reason"]

            main.measurement_store = FakeMeasurementStore(cal, [umik2_input(capture_gain_db=None)])
            assert main._spl_auto_capability()["available"] is False

            main.measurement_store = FakeMeasurementStore(cal, [umik2_input(), umik2_input(id="duplicate")])
            assert main._spl_auto_capability()["available"] is False

            wrong_serial = Path(directory) / "cal-9999999.txt"
            wrong_serial.write_text(valid_header + "10 0\n1000 0\n", encoding="utf-8")
            main.measurement_store = FakeMeasurementStore(wrong_serial, [umik2_input()])
            assert main._spl_auto_capability()["available"] is False

            main.measurement_store = FakeMeasurementStore(cal, [umik2_input()])
            sample_rate = 48000
            t = np.arange(sample_rate * 3) / sample_rate
            sine = 0.05 * np.sqrt(2.0) * np.sin(2.0 * np.pi * 1000.0 * t)
            measured = main._c_weighted_spl_from_capture(sine, sample_rate, -12.11, cal)
            expected = 20.0 * np.log10(0.05) + 124.0 - (-12.11)
            assert abs(measured - expected) < 0.1
            assert abs(measured - (expected + 18.0)) > 17.9
    finally:
        main.measurement_store = original_store
        main._read_measurement_setup_settings = original_settings

    print("UMIK-2 metadata, Sens Factor/AGain/SERNO and fail-closed capture gate: ok")


if __name__ == "__main__":
    main_test()
