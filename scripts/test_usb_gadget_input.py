#!/usr/bin/env python3
"""Gadget capture identity, ordinary mapping and connection-gated auto-select."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import usb_gadget
from audio.usb_gadget_system import GadgetSystem
from audio import custom_input_mapping
from audio.samplerate import overview as overview_mod
from test_external_input_stereo_pairs import _short_line, _detailed_block, _run_command_for, _overview_with
from test_input_auto_select import Harness, X, Z

SOURCE = 'alsa_input.platform-fe980000.usb.stereo-fallback'


class GadgetFixture:
    def setup_files(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.system = GadgetSystem(self.root, run=lambda command: '')
        patcher = patch.object(usb_gadget, 'SYSTEM', self.system)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = self.root / 'sys/devices/platform/fe980000.usb'
        self.device.mkdir(parents=True)
        self.udc = self.root / 'sys/class/udc/fe980000.usb'
        self.udc.mkdir(parents=True)
        (self.udc / 'device').symlink_to(self.device)
        (self.udc / 'state').write_text('not attached\n')
        self.card = self.root / 'sys/class/sound/card3'
        self.card.mkdir(parents=True)
        (self.card / 'device').symlink_to(self.device / 'gadget')
        (self.card / 'id').write_text('UAC2Gadget\n')


class DiscoveryTests(GadgetFixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()

    def test_connection_requires_configured_udc_not_alsa_presence(self):
        for state, connected in [('not attached', False), ('powered', False), ('addressed', False),
                                 ('configured', True), ('suspended', False)]:
            (self.udc / 'state').write_text(state)
            self.assertIs(usb_gadget.capture_connection(3), connected)
        self.assertIsNone(usb_gadget.capture_connection(4))
        (self.udc / 'state').unlink()
        self.assertIs(usb_gadget.capture_connection(3), False)

    def test_card_index_reuse_does_not_mark_normal_usb_interface(self):
        (self.card / 'id').write_text('USB\n')
        (self.card / 'device').unlink()
        (self.card / 'device').symlink_to(self.root / 'sys/devices/pci0000/usb1/1-3/1-3:1.0')
        self.assertIsNone(usb_gadget.capture_connection(3))

    def test_gadget_pairs_and_custom_mapping_keep_normal_routing(self):
        short = _short_line(42, SOURCE, 's32le 4ch 48000Hz')
        detailed = _detailed_block(number=42, name=SOURCE, description='UAC2 Gadget',
                                  spec='s32le 4ch 48000Hz', channel_map='aux0,aux1,aux2,aux3')
        detailed += '\n\t\talsa.card = "3"\n'
        base = custom_input_mapping.card_base(SOURCE)
        mapping = {'name': 'USB reversed', 'channels': {'FL': 2, 'FR': 1, 'SL': 3, 'SR': 4}}
        with patch.object(overview_mod, '_load_custom_input_mappings', return_value={base: mapping}):
            result = _overview_with(_run_command_for(short, detailed))
        first, second, custom = result['inputs']
        self.assertEqual(first['pair_channels'], [1, 2])
        self.assertEqual(second['pair_channels'], [3, 4])
        self.assertEqual(custom['left_channel'], 'AUX1')
        self.assertEqual(custom['right_channel'], 'AUX0')
        self.assertEqual(custom['custom_mapping']['suffixes']['SR'], 'AUX3')
        self.assertTrue(all(entry['usb_gadget'] for entry in result['inputs']))
        self.assertTrue(all(entry['source_key'] == SOURCE for entry in result['inputs']))
        devices = result['custom_mapping_context']['devices']
        self.assertEqual(devices[0]['device']['card_base'], base)
        self.assertEqual(len(devices[0]['capture']['channels']), 4)


class AutoSelectTests(GadgetFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()

    async def test_unconnected_gadget_blocks_presence_even_from_cached_overview(self):
        h = Harness(self, self.tmp.name)
        h.inputs[0]['usb_gadget'] = True
        h.presence[X] = True
        await h.controller.set_armed(X, True)
        await h.tick()
        self.assertEqual(h.switches, [])
        self.assertFalse(h.controller.snapshot()['inputs'][X]['active'])
        self.assertNotIn(h.inputs[0]['source_key'], h.probes)
        (self.udc / 'state').write_text('configured')
        await h.tick()
        self.assertEqual(h.switches, [('external-input', X)])
        (self.udc / 'state').write_text('not attached')
        await h.tick()
        self.assertEqual(h.switches[-1], ('app-playback', None))
        await h.controller.stop()

    async def test_configured_host_without_audio_does_not_switch(self):
        (self.udc / 'state').write_text('configured')
        h = Harness(self, self.tmp.name)
        h.inputs[0]['usb_gadget'] = True
        await h.controller.set_armed(X, True)
        await h.tick()
        self.assertEqual(h.switches, [])
        await h.signal(X, 2)
        self.assertEqual(h.switches, [('external-input', X)])
        await h.controller.stop()

    async def test_normal_input_presence_still_switches_without_udc_connection(self):
        h = Harness(self, self.tmp.name)
        h.inputs[-1]['alsa_card'] = 4
        h.presence[Z] = True
        await h.controller.set_armed(Z, True)
        await h.tick()
        self.assertEqual(h.switches, [('external-input', Z)])
        await h.controller.stop()


if __name__ == '__main__':
    unittest.main()
