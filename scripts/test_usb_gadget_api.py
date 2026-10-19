#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP validation and error propagation with the simulated ConfigFS controller."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import main
from audio import usb_gadget
from audio.usb_gadget_system import GadgetSystem
from test_usb_gadget_system import KernelConfigFS


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.udc = self.root / 'sys/class/udc/controller'
        self.udc.mkdir(parents=True)
        (self.udc / 'state').write_text('not attached')
        (self.root / 'sys/kernel/config/usb_gadget').mkdir(parents=True)
        self.system = GadgetSystem(self.root, run=lambda args: '', fs=KernelConfigFS())
        for replacement in (
            patch.object(usb_gadget, 'SYSTEM', self.system),
            patch.object(usb_gadget, 'set_enabled', self.system.set_enabled),
            patch.object(main.source_transitions, 'read_overview', AsyncMock(return_value={})),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_toggle_reports_observed_and_persistent_state(self):
        self.assertFalse(self.client.get('/api/audio/usb-gadget').json()['enabled'])
        response = self.client.post('/api/audio/usb-gadget', json={'enabled': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['active'])
        self.assertFalse(response.json()['connected'])
        self.assertTrue(self.client.get('/api/audio/usb-gadget').json()['enabled'])
        response = self.client.post('/api/audio/usb-gadget', json={'enabled': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.system.gadget.exists())
        self.assertFalse(self.client.get('/api/audio/usb-gadget').json()['enabled'])

    def test_invalid_bodies_cannot_enable_gadget(self):
        for value in (None, [], 1, {}, {'enabled': 'on'}, {'enabled': 1}, {'enabled': None}):
            with self.subTest(value=value):
                response = self.client.post('/api/audio/usb-gadget', json=value)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(self.system.gadget.exists())
        self.assertFalse(self.system.state_path.exists())

    def test_foreign_controller_is_reported_without_overwrite(self):
        (self.udc / 'function').write_text('existing network gadget')
        response = self.client.post('/api/audio/usb-gadget', json={'enabled': True})
        self.assertEqual(response.status_code, 409)
        self.assertIn('occupied', response.json()['detail'])
        self.assertFalse(self.system.gadget.exists())
        self.assertEqual((self.udc / 'function').read_text(), 'existing network gadget')


if __name__ == '__main__':
    unittest.main()
