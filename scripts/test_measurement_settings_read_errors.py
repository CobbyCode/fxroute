#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Unreadable measurement settings must never become an empty update base."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.file_store import MeasurementFileStore


class MeasurementSettingsReadErrorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="measurement-settings-")
        self.addCleanup(directory.cleanup)
        self.store = MeasurementFileStore(Path(directory.name), has_active_job=lambda: False)
        self.saved = {
            "measure": {"selectedInputId": "mic-a", "activeCalibrationFileId": "cal-a"},
            "other": {"keep": True},
        }

    def test_io_errors_abort_updates_and_preserve_saved_bytes(self):
        original = json.dumps(self.saved).encode("utf-8")
        for error in (PermissionError("access denied"), OSError("read failed")):
            with self.subTest(error=type(error).__name__):
                self.store.settings_path.write_bytes(original)
                updates = []
                with patch.object(Path, "read_text", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        self.store.update_settings(lambda settings: updates.append(settings))
                self.assertIs(raised.exception, error)
                self.assertEqual(updates, [])
                self.assertEqual(self.store.settings_path.read_bytes(), original)

    def test_read_settings_propagates_io_errors(self):
        self.store.settings_path.write_text(json.dumps(self.saved), encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("read failed")):
            with self.assertRaises(OSError):
                self.store.read_settings()

    def assert_invalid_settings_preserved(self, original, error_type):
        self.store.settings_path.write_bytes(original)
        updates = []
        with self.assertRaises(error_type):
            self.store.update_settings(lambda settings: updates.append(settings))
        self.assertEqual(updates, [])
        self.assertEqual(self.store.settings_path.read_bytes(), original)
        with self.assertRaises(error_type):
            self.store.read_settings()

    def test_malformed_json_aborts_update_without_overwrite(self):
        self.assert_invalid_settings_preserved(b'{"measure":', json.JSONDecodeError)

    def test_invalid_utf8_aborts_update_without_overwrite(self):
        self.assert_invalid_settings_preserved(b"\xff", UnicodeDecodeError)

    def test_non_object_json_aborts_update_without_overwrite(self):
        for original in (b"[]", b"null", b"42", b'"settings"'):
            with self.subTest(original=original):
                self.assert_invalid_settings_preserved(original, ValueError)

    def test_missing_settings_can_be_initialized(self):
        self.assertEqual(self.store.read_settings(), {})
        self.store.update_settings(lambda settings: settings.update(self.saved))
        self.assertEqual(self.store.read_settings(), self.saved)

    def test_valid_update_keeps_existing_settings(self):
        self.store.settings_path.write_text(json.dumps(self.saved), encoding="utf-8")

        def update(settings):
            settings["measure"]["selectedInputId"] = "mic-b"

        self.store.update_settings(update)
        self.assertEqual(self.store.read_settings(), {
            "measure": {"selectedInputId": "mic-b", "activeCalibrationFileId": "cal-a"},
            "other": {"keep": True},
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
