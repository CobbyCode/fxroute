#!/usr/bin/env python3
"""Keep interactive identity unset and automated identity complete."""
import pathlib
import unittest
import yaml

ROOT = pathlib.Path(__file__).parent / 'autoinstall'


class ProductSeedTest(unittest.TestCase):
    def test_interactive_identity_has_no_partial_defaults(self):
        config = yaml.safe_load((ROOT / 'user-data').read_text())['autoinstall']
        self.assertIn('identity', config['interactive-sections'])
        self.assertIn('network', config['interactive-sections'])
        self.assertNotIn('identity', config,
                         'Interactive identity must be entered in the installer')

    def test_automated_identity_has_all_required_fields(self):
        config = yaml.safe_load((ROOT / 'user-data.test').read_text())['autoinstall']
        for field in ('username', 'hostname', 'password'):
            self.assertTrue(config['identity'][field])


if __name__ == '__main__':
    unittest.main()
