#!/usr/bin/env python3
"""Product profiles share defaults, never automated identity or network."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).parent


class ProductSeedTest(unittest.TestCase):
    def generate(self, test_seed=False):
        generator = ROOT / 'generate-seeds.py'
        self.assertTrue(generator.exists(), 'Missing per-profile seed generator')
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(generator), '--output-dir', directory]
            if test_seed:
                command.append('--test-seed')
            subprocess.run(command, check=True)
            paths = sorted(pathlib.Path(directory).iterdir())
            self.assertEqual([p.name for p in paths], ['desktop.yaml', 'headless.yaml'])
            return {p.stem: yaml.safe_load(p.read_text()) for p in paths}

    def test_product_profiles_are_interactive_without_identity_defaults(self):
        for profile, document in self.generate().items():
            with self.subTest(profile=profile):
                self.assertEqual(list(document), ['autoinstall'])
                config = document['autoinstall']
                self.assertEqual(set(config['interactive-sections']),
                                 {'keyboard', 'network', 'identity'})
                self.assertNotIn('identity', config)
                self.assertNotIn('network', config)
                self.assertEqual(config['source']['id'], 'ubuntu-desktop-minimal')

    def test_profile_markers_and_headless_helper_after_payload_copy(self):
        for profile, document in self.generate().items():
            commands = document['autoinstall']['late-commands']
            self.assertIn(f'echo {profile} > /target/etc/fxroute-iso-profile', commands)
            self.assertIn('curtin in-target --target=/target -- systemctl enable fxroute-first-boot.service', commands)
            helper = 'curtin in-target --target=/target -- /opt/fxroute-iso/scripts/prepare-headless-target.sh'
            if profile == 'headless':
                self.assertIn(helper, commands)
                self.assertLess(commands.index('cp -r /cdrom/fxroute-iso /target/opt/fxroute-iso'), commands.index(helper))
                self.assertLess(commands.index('echo headless > /target/etc/fxroute-iso-profile'), commands.index(helper))
            else:
                self.assertNotIn(helper, commands)

    def test_test_seed_automates_both_profiles_but_keeps_product_late_commands(self):
        product = self.generate()
        for profile, document in self.generate(test_seed=True).items():
            config = document['autoinstall']
            self.assertNotIn('interactive-sections', config)
            for field in ('username', 'hostname', 'password'):
                self.assertTrue(config['identity'][field])
            self.assertEqual(config['late-commands'], product[profile]['autoinstall']['late-commands'])

    def test_common_base_is_interactive_without_partial_identity(self):
        config = yaml.safe_load((ROOT / 'autoinstall/user-data').read_text())['autoinstall']
        self.assertEqual(set(config['interactive-sections']), {'keyboard', 'network', 'identity'})
        self.assertNotIn('identity', config)


if __name__ == '__main__':
    unittest.main()
