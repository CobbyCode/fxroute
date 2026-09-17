#!/usr/bin/env python3
"""Exercise the first-boot dconf setup in an isolated directory."""
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parent / 'scripts/first-boot-install-ubuntu.sh'


class DesktopDefaultsTest(unittest.TestCase):
    def test_profile_enables_local_database_without_losing_existing_entries(self):
        source = SCRIPT.read_text()
        block = source.split('  # The appliance never suspends', 1)[1]
        block = '  # The appliance never suspends' + block.split('  # systemd-logind:', 1)[0]
        for initial in (None, 'user-db:user\nsystem-db:site\n'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                profile = root / 'profile/user'
                if initial is not None:
                    profile.parent.mkdir(parents=True)
                    profile.write_text(initial)
                isolated = block.replace('/etc/dconf', temp)
                command = 'set -eu\ndconf() { :; }\ndconf_dir=' + temp + '/db/local.d\n' + isolated
                subprocess.run(['bash', '-c', command], check=True)
                self.assertTrue(profile.exists(), 'The local database must be registered in a dconf profile')
                lines = profile.read_text().splitlines()
                self.assertEqual(lines[0], 'user-db:user')
                self.assertIn('system-db:local', lines)
                if initial:
                    self.assertIn('system-db:site', lines)
                subprocess.run(['bash', '-c', command], check=True)
                self.assertEqual(profile.read_text().splitlines().count('system-db:local'), 1)
                self.assertIn('idle-delay=uint32 0', (root / 'db/local.d/10-fxroute-appliance').read_text())


if __name__ == '__main__':
    unittest.main()
