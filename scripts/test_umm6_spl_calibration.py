#!/usr/bin/env python3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import measurement_session
import spl_calibration


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "1880171.txt"


class FakeMeasurementStore:
    def __init__(self, inputs, calibration_path=FIXTURE):
        self.inputs = inputs
        self.calibration_path = calibration_path

    def get_calibration_state(self):
        return {
            "active_calibration_file_id": self.calibration_path.name,
            "calibrations": [{
                "id": self.calibration_path.name,
                "filename": self.calibration_path.name,
                "path": str(self.calibration_path),
            }],
        }

    def list_inputs(self):
        return {"inputs": self.inputs}


def umm6_input(**updates):
    result = {
        "id": "pw-source-umm6",
        "node_name": "alsa_input.usb-Dayton_Audio_UMM-6.analog-stereo",
        "node_description": "Dayton Audio UMM-6 IPGA +30 dB",
        "device_name": "alsa_card.usb-Dayton_Audio_UMM-6",
        "device_description": "Dayton Audio UMM-6",
        "device_product_name": "UMM-6",
        "device_vendor_id": "0x0d8c",
        "device_product_id": "0x0147",
        "alsa_card_name": "UMM-6",
        "alsa_long_card_name": "Dayton Audio UMM-6 input gain +30 dB",
        "capture_volume_percent": 100.0,
        "capture_gain_db": 0.0,
    }
    result.update(updates)
    return result


def main_test():
    original_store = main.measurement_store
    original_settings = measurement_session._read_measurement_setup_settings
    try:
        measurement_session._read_measurement_setup_settings = lambda: {
            "selectedInputId": "pw-source-umm6",
            "selectedMicInputChannel": "1",
            "selectedReferenceInputChannel": "",
        }
        main.measurement_store = FakeMeasurementStore([umm6_input()])

        assert spl_calibration._UMM6_PROFILE.parse_calibration_header(FIXTURE) == {
            "sensitivity_factor_db": -19.59,
            "serial_number": "1880171",
        }
        curve = spl_calibration._UMM6_PROFILE.parse_calibration_curve(FIXTURE)
        assert curve is not None and len(curve) == 3
        frequencies, corrections, phases = curve
        assert len(frequencies) == 15
        assert corrections[8] == -0.01
        assert phases[-1] == -11.37

        assert spl_calibration._is_umm6_input(umm6_input()) is True
        assert spl_calibration._is_umm6_input(umm6_input(device_vendor_id="0x2752")) is False
        assert spl_calibration._is_umm6_input(umm6_input(device_product_id="0x0007")) is False
        assert spl_calibration._is_umm6_input(umm6_input(
            node_name="generic", node_description="generic", device_name="generic",
            device_description="generic", device_product_name="generic",
            alsa_card_name="generic", alsa_long_card_name="generic",
        )) is False

        capability = spl_calibration._spl_auto_capability()
        assert capability["available"] is True
        assert capability["microphone_model"] == "Dayton UMM-6"
        assert capability["reference_sensitivity_dbfs_per_pa"] == -19.0
        assert capability["reference_ipga_db"] == 30.0
        assert capability["checks"]["capture_reference_state"] is True

        fail_closed_cases = [
            (umm6_input(capture_gain_db=-6.0), "capture gain"),
            (umm6_input(capture_volume_percent=None), "cannot be verified"),
        ]
        for input_item, expected_reason in fail_closed_cases:
            main.measurement_store = FakeMeasurementStore([input_item])
            failed = spl_calibration._spl_auto_capability()
            assert failed["available"] is False
            assert expected_reason in failed["reason"]

        main.measurement_store = FakeMeasurementStore([umm6_input(), umm6_input(id="duplicate")])
        assert spl_calibration._spl_auto_capability()["available"] is False

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            wrong_serial = Path(directory) / "9999999.txt"
            wrong_serial.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            main.measurement_store = FakeMeasurementStore([umm6_input()], wrong_serial)
            assert spl_calibration._spl_auto_capability()["available"] is False

            malformed = Path(directory) / "1880171.txt"
            malformed.write_text(
                '"Sens Factor =-19.590dB, SERNO: 1880171"\n10 0\n1000 0\n',
                encoding="utf-8",
            )
            main.measurement_store = FakeMeasurementStore([umm6_input()], malformed)
            failed = spl_calibration._spl_auto_capability()
            assert failed["available"] is False
            assert "frequency/correction/phase" in failed["reason"]

        main.measurement_store = FakeMeasurementStore([umm6_input()])
        sample_rate = 48000
        time = np.arange(sample_rate * 3) / sample_rate
        sine = 0.05 * np.sqrt(2.0) * np.sin(2.0 * np.pi * 1000.0 * time)
        measured = spl_calibration._c_weighted_spl_from_capture(
            sine, sample_rate, -19.59, FIXTURE, profile=spl_calibration._UMM6_PROFILE
        )
        expected = 20.0 * np.log10(0.05) + 94.0 - (-19.59)
        assert abs(measured - expected) < 0.1
    finally:
        main.measurement_store = original_store
        measurement_session._read_measurement_setup_settings = original_settings

    print("UMM-6 metadata, fixture header/phase curve, +30 dB capture gate and SPL reference: ok")


if __name__ == "__main__":
    main_test()
