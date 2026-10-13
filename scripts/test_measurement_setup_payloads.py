#!/usr/bin/env python3
"""Behavior tests for the REFACTOR-004 extraction:

- measurement.normalize_measurement_optional_input_channel
- measurement.measurement_setup_settings_from_payload

plus wrapper parity against measurement_session._normalize_measurement_optional_input_channel
and measurement_session._measurement_setup_settings_from_payload.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import measurement.session as measurement_session
from measurement.store import (
    MeasurementStore,
    measurement_input_persistent_id,
    measurement_setup_settings_from_payload,
    normalize_measurement_optional_input_channel,
    resolve_measurement_sample_rate,
    resolve_measurement_input_selection,
)


class NormalizeOptionalInputChannelTests(unittest.TestCase):
    def test_none_and_empty_return_empty(self):
        for value in (None, ""):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")

    def test_positive_int_normalized(self):
        self.assertEqual(normalize_measurement_optional_input_channel(3), "3")
        self.assertEqual(normalize_measurement_optional_input_channel(1), "1")
        self.assertEqual(normalize_measurement_optional_input_channel("12"), "12")

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_measurement_optional_input_channel("  4  "), "4")

    def test_zero_and_negative_return_empty(self):
        for value in (0, -1, "0", "-2"):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")

    def test_invalid_values_return_empty(self):
        for value in ("abc", "3.5", 3.5, True, [1], {}):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")


class SetupSettingsFromPayloadTests(unittest.TestCase):
    def test_empty_payload_returns_defaults(self):
        self.assertEqual(
            measurement_setup_settings_from_payload({}),
            {
                "selectedInputId": "",
                "selectedInputKey": "",
                "selectedInputConfigured": False,
                "selectedMicInputChannel": "1",
                "selectedReferenceInputChannel": "",
                "selectedReferenceInputChannelLeft": "",
                "selectedReferenceInputChannelRight": "",
            },
        )

    def test_measure_not_a_dict_treated_as_empty(self):
        for payload in ({"measure": "x"}, {"measure": [1, 2]}, {"measure": None}):
            self.assertEqual(
                measurement_setup_settings_from_payload(payload),
                {
                    "selectedInputId": "",
                    "selectedInputKey": "",
                    "selectedInputConfigured": False,
                    "selectedMicInputChannel": "1",
                    "selectedReferenceInputChannel": "",
                    "selectedReferenceInputChannelLeft": "",
                    "selectedReferenceInputChannelRight": "",
                },
            )

    def test_camel_case_keys_normalized(self):
        payload = {
            "measure": {
                "selectedInputId": "mic-1",
                "selectedMicInputChannel": 3,
                "selectedReferenceInputChannel": "2",
            }
        }
        self.assertEqual(
            measurement_setup_settings_from_payload(payload),
            {
                "selectedInputId": "mic-1",
                "selectedInputKey": "",
                "selectedInputConfigured": True,
                "selectedMicInputChannel": "3",
                    "selectedReferenceInputChannel": "2",
                    "selectedReferenceInputChannelLeft": "",
                    "selectedReferenceInputChannelRight": "",
                },
        )

    def test_rate_stored_by_the_removed_select_is_not_reported(self):
        payload = {"measure": {"selectedInputId": "mic-1", "measurementSampleRate": 96000}}
        self.assertNotIn("measurementSampleRate", measurement_setup_settings_from_payload(payload))

    def test_snake_case_reference_key_fallback(self):
        # reference_input_channel is only a fallback when
        # selectedReferenceInputChannel is missing or None.
        payload = {"measure": {"selectedReferenceInputChannel": "4", "reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "4")

        payload = {"measure": {"reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "9")

        payload = {"measure": {"selectedReferenceInputChannel": None, "reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "9")

    def test_split_reference_channels_normalized(self):
        payload = {
            "measure": {
                "selectedReferenceInputChannelLeft": "2",
                "selectedReferenceInputChannelRight": "3",
            }
        }
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannelLeft"], "2")
        self.assertEqual(result["selectedReferenceInputChannelRight"], "3")

    def test_split_reference_channels_ignore_invalid_values(self):
        payload = {
            "measure": {
                "selectedReferenceInputChannelLeft": 0,
                "selectedReferenceInputChannelRight": "abc",
            }
        }
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannelLeft"], "")
        self.assertEqual(result["selectedReferenceInputChannelRight"], "")

    def test_invalid_channel_values_handled(self):
        payload = {
            "measure": {
                "selectedMicInputChannel": 0,
                "selectedReferenceInputChannel": "abc",
            }
        }
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedMicInputChannel"], "1")  # default fallback
        self.assertEqual(result["selectedReferenceInputChannel"], "")

    def test_input_payload_not_mutated(self):
        payload = {"measure": {"selectedInputId": "x", "selectedMicInputChannel": 2}}
        before = copy.deepcopy(payload)
        measurement_setup_settings_from_payload(payload)
        self.assertEqual(payload, before)

    def test_selected_input_id_stringified(self):
        result = measurement_setup_settings_from_payload({"measure": {"selectedInputId": 42}})
        self.assertEqual(result["selectedInputId"], "42")
        result = measurement_setup_settings_from_payload({"measure": {"selectedInputId": None}})
        self.assertEqual(result["selectedInputId"], "")

    def test_stable_input_key_is_returned(self):
        result = measurement_setup_settings_from_payload({
            "measure": {"selectedInputId": "pw-source-54", "selectedInputKey": "device-serial:UMIK-123"}
        })
        self.assertEqual(result["selectedInputKey"], "device-serial:UMIK-123")
        self.assertTrue(result["selectedInputConfigured"])


class MeasurementInputPersistenceTests(unittest.TestCase):
    @staticmethod
    def _umik(input_id):
        return {
            "id": input_id,
            "device_serial": "miniDSP-UMIK-123",
            "node_name": "alsa_input.usb-miniDSP_UMIK-1_123.analog-stereo",
        }

    def test_same_serial_resolves_after_pipewire_node_id_changes(self):
        old_input = self._umik("pw-source-54")
        stable_id = measurement_input_persistent_id(old_input)
        new_input = self._umik("pw-source-65")
        new_input["persistent_id"] = measurement_input_persistent_id(new_input)

        selection = resolve_measurement_input_selection(
            [new_input],
            {"selectedInputId": "pw-source-54", "selectedInputKey": stable_id},
        )

        self.assertEqual(selection["input_id"], "pw-source-65")
        self.assertEqual(selection["persistent_id"], stable_id)
        self.assertFalse(selection["unavailable"])

    def test_saved_selection_survives_discovery_without_device_serial(self):
        # A failed/timeout `wpctl inspect <device.id>` drops device_serial, so
        # discovery reports the same microphone under the bare node-name id
        # while the stored key still carries the device-serial prefix.
        stored_key = measurement_input_persistent_id(self._umik("pw-source-54"))
        discovered = {
            "id": "pw-source-111",
            "node_name": "alsa_input.usb-miniDSP_UMIK-1_123.analog-stereo",
        }
        discovered["persistent_id"] = measurement_input_persistent_id(discovered)
        self.assertNotEqual(discovered["persistent_id"], stored_key)

        selection = resolve_measurement_input_selection(
            [discovered],
            {"selectedInputId": "pw-source-111", "selectedInputKey": stored_key},
        )

        self.assertEqual(selection["input_id"], "pw-source-111")
        self.assertFalse(selection["unavailable"])
        # The resolved key is the current shape, so the frontend auto-heal
        # persists it and the selection converges instead of flapping.
        self.assertEqual(selection["persistent_id"], discovered["persistent_id"])

    def test_stale_saved_key_still_falls_back_to_present_legacy_id(self):
        # The stored key names hardware that is gone, but the legacy id still
        # resolves to a present source: that source wins over "unavailable".
        present = self._umik("pw-source-111")
        present["persistent_id"] = measurement_input_persistent_id(present)

        selection = resolve_measurement_input_selection(
            [present],
            {"selectedInputId": "pw-source-111", "selectedInputKey": "device-serial:REMOVED-999"},
        )

        self.assertEqual(selection["input_id"], "pw-source-111")
        self.assertFalse(selection["unavailable"])

    def test_missing_saved_source_never_falls_back_to_first_input(self):
        other = {
            "id": "pw-source-10",
            "device_serial": "OTHER-456",
            "node_name": "alsa_input.usb-Other_Microphone.analog-stereo",
        }
        other["persistent_id"] = measurement_input_persistent_id(other)

        selection = resolve_measurement_input_selection(
            [other],
            {"selectedInputId": "pw-source-54", "selectedInputKey": "device-serial:miniDSP-UMIK-123"},
        )

        self.assertEqual(selection["input_id"], "")
        self.assertTrue(selection["unavailable"])
        self.assertNotEqual(selection["input_id"], other["id"])

    def test_start_resolution_uses_stable_key_not_reused_old_node_id(self):
        selected = self._umik("pw-source-65")
        selected["persistent_id"] = measurement_input_persistent_id(selected)
        wrong = {
            "id": "pw-source-54",
            "device_serial": "OTHER-456",
            "node_name": "alsa_input.usb-Other_Microphone.analog-stereo",
        }
        wrong["persistent_id"] = measurement_input_persistent_id(wrong)

        resolved = MeasurementStore._resolve_capture_input(
            [wrong, selected],
            input_id="pw-source-54",
            input_key=selected["persistent_id"],
        )

        self.assertEqual(resolved["id"], "pw-source-65")

    def test_first_time_setup_may_select_first_input(self):
        first = self._umik("pw-source-65")
        first["persistent_id"] = measurement_input_persistent_id(first)

        selection = resolve_measurement_input_selection([first], {})

        self.assertEqual(selection["input_id"], "pw-source-65")
        self.assertFalse(selection["configured"])
        self.assertFalse(selection["unavailable"])

    def test_rediscovered_device_clears_unavailable_state(self):
        settings = {
            "selectedInputId": "pw-source-54",
            "selectedInputKey": measurement_input_persistent_id(self._umik("pw-source-54")),
        }

        missing = resolve_measurement_input_selection([], settings)
        self.assertTrue(missing["unavailable"])
        self.assertEqual(missing["input_id"], "")

        rediscovered = self._umik("pw-source-65")
        rediscovered["persistent_id"] = measurement_input_persistent_id(rediscovered)
        found = resolve_measurement_input_selection([rediscovered], settings)

        self.assertFalse(found["unavailable"])
        self.assertEqual(found["input_id"], "pw-source-65")

    def test_switching_between_two_present_devices_never_marks_unavailable(self):
        first = self._umik("pw-source-54")
        first["persistent_id"] = measurement_input_persistent_id(first)
        second = {
            "id": "pw-source-70",
            "device_serial": "OTHER-456",
            "node_name": "alsa_input.usb-Other_Microphone.analog-stereo",
        }
        second["persistent_id"] = measurement_input_persistent_id(second)
        inputs = [first, second]

        selection = resolve_measurement_input_selection(
            inputs,
            {"selectedInputId": "pw-source-54", "selectedInputKey": first["persistent_id"]},
        )
        self.assertEqual(selection["input_id"], "pw-source-54")
        self.assertFalse(selection["unavailable"])

        selection = resolve_measurement_input_selection(
            inputs,
            {"selectedInputId": "pw-source-70", "selectedInputKey": second["persistent_id"]},
        )
        self.assertEqual(selection["input_id"], "pw-source-70")
        self.assertFalse(selection["unavailable"])

    def test_48_khz_wins_whenever_the_input_offers_it(self):
        for rates in ([44100, 48000], [48000], [44100, 48000, 96000, 192000], [48000, 88200, 96000]):
            for running in (44100, 96000, None):
                with self.subTest(rates=rates, running=running):
                    self.assertEqual(
                        resolve_measurement_sample_rate({"supported_rates": rates}, lambda: running), 48000)

    def test_input_without_rates_uses_its_source_rate_or_48_khz(self):
        self.assertEqual(resolve_measurement_sample_rate({}), 48000)
        self.assertEqual(resolve_measurement_sample_rate({"sample_rate": 96000}), 96000)

    def test_input_without_48_khz_prefers_the_running_rate(self):
        hi_res_tier = {"supported_rates": [88200, 96000]}
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: 96000), 96000)
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: 88200), 88200)

    def test_running_rate_the_input_cannot_run_falls_back_to_the_lowest_rate(self):
        hi_res_tier = {"supported_rates": [88200, 96000]}
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: 192000), 88200)
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: None), 88200)
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier), 88200)
        self.assertEqual(resolve_measurement_sample_rate({"supported_rates": [96000]}), 96000)

    def test_48_khz_on_a_reachable_tier_wins_over_the_running_rate(self):
        hi_res_tier = {"supported_rates": [88200, 96000]}
        self.assertEqual(
            resolve_measurement_sample_rate(
                hi_res_tier, lambda: 96000, lambda: [44100, 48000, 88200, 96000, 176400, 192000]),
            48000,
        )
        # Tiers without 48 kHz change nothing.
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: 96000, lambda: [88200, 96000]), 96000)
        self.assertEqual(resolve_measurement_sample_rate(hi_res_tier, lambda: 96000, lambda: []), 96000)

    def test_offered_48_khz_reads_neither_the_running_rate_nor_the_tiers(self):
        def unread():
            raise AssertionError("must not be read")

        self.assertEqual(resolve_measurement_sample_rate({"supported_rates": [44100, 48000]}, unread, unread), 48000)


class StoreMeasurementRateTests(unittest.TestCase):
    """The store resolves the entry rate and the sweep rate from the same rule."""

    SCARLETT = {
        "id": "pw-source-89",
        "device_serial": "SCARLETT-1",
        "node_name": "alsa_input.usb-Focusrite_Scarlett-00.pro-input-0",
        "available": True,
        "supported_rates": [88200, 96000],
    }
    ONBOARD = {
        "id": "pw-source-60",
        "node_name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "available": True,
        "supported_rates": [176400, 192000],
    }
    UMIK = {
        "id": "pw-source-70",
        "device_serial": "UMIK-1",
        "node_name": "alsa_input.usb-miniDSP_Umik-1.analog-stereo",
        "available": True,
        "supported_rates": [48000],
    }
    SCARLETT_TIERS = {"device": "usb-Focusrite_Scarlett-00", "rates": [44100, 48000, 88200, 96000, 176400, 192000]}

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": tempdir.name, "XDG_STATE_HOME": tempdir.name}):
            self.store = MeasurementStore(home=Path(tempdir.name))
        self.selected = self.SCARLETT
        self.store._discover_capture_inputs = lambda: [dict(self.SCARLETT), dict(self.ONBOARD), dict(self.UMIK)]
        # A rate stored by the removed rate select is no longer read.
        self.store._read_settings = lambda: {"measure": {
            "selectedInputId": self.selected["id"],
            "selectedInputKey": measurement_input_persistent_id(self.selected),
            "measurementSampleRate": 88200,
        }}
        self.running = 96000
        self.reads = 0
        self.tier_reads = 0

        def provider():
            self.reads += 1
            return self.running

        def tiers():
            self.tier_reads += 1
            return dict(self.SCARLETT_TIERS)

        self.provider = provider
        self.tiers = tiers

    def _sweep_rates(self):
        annotated = self.store._measurement_inputs_with_sample_rate(self.store._discover_capture_inputs())
        return {item["id"]: item["measurement_sample_rate"] for item in annotated}

    def test_store_without_providers_uses_the_lowest_rate(self):
        self.assertIsNone(self.store.running_sample_rate_provider)
        self.assertIsNone(self.store.tier_rates_provider)
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 88200)
        self.assertEqual(self._sweep_rates()["pw-source-89"], 88200)

    def test_entry_rate_and_sweep_rate_follow_the_running_rate(self):
        self.store.running_sample_rate_provider = self.provider
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 96000)
        annotated = self.store._measurement_inputs_with_sample_rate(self.store._discover_capture_inputs())
        scarlett = next(item for item in annotated if item["id"] == "pw-source-89")
        self.assertEqual(scarlett["measurement_sample_rate"], 96000)
        self.assertEqual(scarlett["sample_rate"], 96000)
        self.assertEqual(scarlett["supported_rates"], [88200, 96000])

    def test_resolution_is_stable_once_the_session_runs_its_rate(self):
        # The hardware runs the entry rate afterwards, so every later sweep of
        # the same window resolves the same rate again.
        self.store.running_sample_rate_provider = self.provider
        for self.running in (96000, 88200):
            entry_rate = self.store._resolve_measurement_sample_rate()
            self.assertEqual(entry_rate, self.running)
            self.assertEqual(self._sweep_rates()["pw-source-89"], entry_rate)

    def test_running_rate_outside_the_input_uses_the_lowest_rate(self):
        self.store.running_sample_rate_provider = self.provider
        self.running = 44100
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 88200)
        self.assertEqual(self._sweep_rates(), {"pw-source-89": 88200, "pw-source-60": 176400, "pw-source-70": 48000})

    def test_input_offering_48_khz_reads_no_provider(self):
        self.store.running_sample_rate_provider = self.provider
        self.store.tier_rates_provider = self.tiers
        self.selected = self.UMIK
        self.store._discover_capture_inputs = lambda: [dict(self.UMIK)]
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 48000)
        self.assertEqual(self._sweep_rates(), {"pw-source-70": 48000})
        self.assertEqual((self.reads, self.tier_reads), (0, 0))

    def test_stored_rate_of_the_removed_select_is_ignored(self):
        self.selected = dict(self.SCARLETT, supported_rates=[48000, 88200, 96000])
        self.store._discover_capture_inputs = lambda: [dict(self.selected)]
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 48000)
        self.assertEqual(self._sweep_rates(), {"pw-source-89": 48000})

    def test_one_listing_reads_the_running_rate_once(self):
        self.store.running_sample_rate_provider = self.provider
        self.running = 192000
        rates = self._sweep_rates()
        # Both inputs without 48 kHz need the fallback; the unselected one
        # follows the same rule as the selected one.
        self.assertEqual(rates, {"pw-source-89": 88200, "pw-source-60": 192000, "pw-source-70": 48000})
        self.assertEqual(self.reads, 1)

    def test_failing_provider_uses_the_lowest_rate(self):
        def failing():
            raise RuntimeError("pw-metadata unavailable")

        self.store.running_sample_rate_provider = failing
        with self.assertLogs("measurement.store", level="WARNING") as logs:
            self.assertEqual(self.store._resolve_measurement_sample_rate(), 88200)
        self.assertIn("pw-metadata unavailable", logs.output[0])

    def test_invalid_provider_reading_uses_the_lowest_rate(self):
        for reading in (None, 0, -96000, "96000", 96000.0, True):
            self.store.running_sample_rate_provider = lambda: reading
            with self.subTest(reading=reading):
                self.assertEqual(self.store._resolve_measurement_sample_rate(), 88200)

    def test_reachable_tier_moves_the_devices_own_input_to_48_khz(self):
        # Auto policy: the device sits on its 88.2/96 kHz tier, the entry moves
        # it to the tier that carries 48 kHz.
        self.store.running_sample_rate_provider = self.provider
        self.store.tier_rates_provider = self.tiers
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 48000)
        self.assertEqual(self._sweep_rates(), {"pw-source-89": 48000, "pw-source-60": 176400, "pw-source-70": 48000})

    def test_one_listing_reads_the_tiers_once(self):
        self.store.tier_rates_provider = self.tiers
        self._sweep_rates()
        self.assertEqual(self.tier_reads, 1)

    def test_tiers_of_another_device_do_not_apply(self):
        self.store.running_sample_rate_provider = self.provider
        self.store.tier_rates_provider = lambda: {"device": "usb-Other_Interface-00", "rates": [44100, 48000]}
        self.assertEqual(self.store._resolve_measurement_sample_rate(), 96000)
        self.assertEqual(self._sweep_rates()["pw-source-89"], 96000)

    def test_no_reachable_tier_keeps_the_running_rate(self):
        # Fixed policy: the hook reports no tier to move to.
        self.store.running_sample_rate_provider = self.provider
        for reach in (None, {}, {"device": "usb-Focusrite_Scarlett-00", "rates": [88200, 96000]}, "auto"):
            self.store.tier_rates_provider = lambda: reach
            with self.subTest(reach=reach):
                self.assertEqual(self.store._resolve_measurement_sample_rate(), 96000)

    def test_failing_tier_provider_keeps_the_running_rate(self):
        def failing():
            raise RuntimeError("overview unavailable")

        self.store.running_sample_rate_provider = self.provider
        self.store.tier_rates_provider = failing
        with self.assertLogs("measurement.store", level="WARNING") as logs:
            self.assertEqual(self.store._resolve_measurement_sample_rate(), 96000)
        self.assertIn("overview unavailable", logs.output[0])


class RetiredMeasurementRateSettingTests(unittest.TestCase):
    """The settings update no longer stores a measurement rate."""

    def test_patch_with_a_measurement_rate_stores_none(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}):
                store = MeasurementStore(home=Path(tempdir))
            services = mock.Mock(get_store=lambda: store)
            with mock.patch.object(measurement_session, "_measurement_services", return_value=services):
                result = measurement_session._update_measurement_setup_settings(
                    {"selectedInputId": "mic-a", "measurementSampleRate": 96000, "measurement_sample_rate": 96000})
            self.assertEqual(result["selectedInputId"], "mic-a")
            self.assertNotIn("measurementSampleRate", result)
            self.assertEqual(store.read_settings()["measure"], {"selectedInputId": "mic-a"})


class MeasurementTierRatesTests(unittest.TestCase):
    """The application hook reports the selected output's tiers under Auto only."""

    OVERVIEW = {"selected_output": {
        "key": "alsa_output.usb-Focusrite_Scarlett-00.pro-output-0",
        "device_profile": {"active_tier": "14ch", "tiers": [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
        ]},
    }}

    def _tier_rates(self, policy, overview):
        with mock.patch.object(main.samplerate, "load_sample_rate_policy", return_value=policy), \
                mock.patch.object(main, "get_audio_output_overview", return_value=overview) as read:
            return main._measurement_tier_rates(), read.call_count

    def test_auto_policy_reports_the_selected_outputs_tiers(self):
        reach, _ = self._tier_rates({"mode": "auto", "rate": None}, self.OVERVIEW)
        self.assertEqual(reach, {"device": "usb-Focusrite_Scarlett-00", "rates": [44100, 48000, 88200, 96000]})

    def test_fixed_policy_reports_nothing_and_reads_no_output(self):
        reach, reads = self._tier_rates({"mode": "fixed", "rate": 96000}, self.OVERVIEW)
        self.assertIsNone(reach)
        self.assertEqual(reads, 0)

    def test_output_without_tiers_reports_nothing(self):
        plain = {"selected_output": {"key": "alsa_output.usb-Plain_DAC-00.analog-stereo", "device_profile": None}}
        for overview in (plain, {"selected_output": None}, {}):
            with self.subTest(overview=overview):
                self.assertIsNone(self._tier_rates({"mode": "auto", "rate": None}, overview)[0])

    def test_reported_tiers_match_the_devices_own_input(self):
        reach, _ = self._tier_rates({"mode": "auto", "rate": None}, self.OVERVIEW)
        store_module = sys.modules[MeasurementStore.__module__]
        own = {"node_name": "alsa_input.usb-Focusrite_Scarlett-00.pro-input-0"}
        other = {"node_name": "alsa_input.usb-miniDSP_Umik-1.analog-stereo"}
        self.assertEqual(store_module._input_tier_rates(own, reach), [44100, 48000, 88200, 96000])
        self.assertEqual(store_module._input_tier_rates(other, reach), [])
        self.assertEqual(store_module._input_tier_rates({}, reach), [])


class RunningOutputSampleRateTests(unittest.TestCase):
    def test_reports_the_active_rate_of_the_selected_output(self):
        with mock.patch.object(main, "get_samplerate_status", return_value={"active_rate": 96000, "force_rate": 0}):
            self.assertEqual(main._running_output_sample_rate(), 96000)

    def test_unknown_active_rate_is_none(self):
        for status in ({"active_rate": None}, {"status": "error"}, {"active_rate": 0}, {"active_rate": "96000"}):
            with self.subTest(status=status), mock.patch.object(main, "get_samplerate_status", return_value=status):
                self.assertIsNone(main._running_output_sample_rate())


class WrapperParityTests(unittest.TestCase):
    def test_normalize_wrapper_matches_module_function(self):
        for value in (None, "", " 3 ", 3, 0, -1, "abc", 1, "0", " 12 ", 3.5, True):
            self.assertEqual(
                measurement_session._normalize_measurement_optional_input_channel(value),
                normalize_measurement_optional_input_channel(value),
                f"Parität für {value!r}",
            )

    def test_settings_wrapper_matches_module_function(self):
        payloads = [
            {},
            {"measure": {}},
            {"measure": {"selectedInputId": "mic1", "selectedMicInputChannel": 3, "selectedReferenceInputChannel": "2"}},
            {"measure": {"input_id": "x", "mic_input_channel": "4", "reference_input_channel": "5"}},
            {"measure": {"selectedReferenceInputChannel": None, "reference_input_channel": "7"}},
            {"measure": "not-a-dict"},
            {"measure": {"selectedMicInputChannel": None, "selectedReferenceInputChannel": 0}},
        ]
        for payload in payloads:
            self.assertEqual(
                measurement_session._measurement_setup_settings_from_payload(payload),
                measurement_setup_settings_from_payload(payload),
                f"Parität für {payload!r}",
            )


if __name__ == "__main__":
    unittest.main()
