#!/usr/bin/env python3
"""Run target preparation and first boot with isolated files and fake system tools."""
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).parent / 'scripts'

# Package/system operations must never touch the test host. The fake tools retain
# package state so verification exercises the script's actual removal decisions.
FAKE_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'commands').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
packages_file = root / 'packages.json'
packages = json.loads(packages_file.read_text())
if name == 'id':
    print('0' if args == ['-u'] else '1000')
elif name == 'dpkg-query':
    for package, status in packages.items():
        print(package + '\t' + status)
elif name == 'apt-get':
    selected = args[args.index('purge') + 1:]
    if '--simulate' in args:
        for package in selected + json.loads(os.environ.get('EXTRA_REMOVALS', '[]')):
            print('Purg ' + package + ' [1.0]')
        sys.exit(int(os.environ.get('APT_SIMULATE_STATUS', '0')))
    if os.environ.get('APT_PURGE_FAIL'):
        sys.exit(1)
    for package in selected:
        if not os.environ.get('APT_LEAVES_PACKAGES'):
            packages.pop(package, None)
    packages_file.write_text(json.dumps(packages))
elif name == 'snap':
    assert args == ['remove', '--purge', 'firefox'], args
    if os.environ.get('SNAP_REMOVE_FAIL'):
        sys.exit(1)
    state_file = root / 'var/lib/snapd/state.json'
    state = json.loads(state_file.read_text())
    state['data']['snaps'].pop('firefox', None)
    state_file.write_text(json.dumps(state))
elif name == 'getent':
    print('fxroute:x:1000:1000:FXRoute:' + str(root / 'home/fxroute') + ':/bin/bash')
elif name == 'tar':
    target = pathlib.Path(args[args.index('--directory') + 1])
    (target / 'install.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$TEST_ROOT/installer-args"\n')
elif name not in ('systemctl', 'sshd', 'runuser', 'git'):
    raise RuntimeError('Unexpected tool: ' + name)
'''


class IsolatedScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fxroute-headless-')
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('id', 'dpkg-query', 'apt-get', 'snap', 'systemctl',
                     'getent', 'tar', 'sshd', 'runuser', 'git'):
            tool = self.bin / name
            tool.write_text(FAKE_TOOL)
            tool.chmod(0o755)
        self.packages = self.root / 'packages.json'
        self.packages.write_text('{}')
        self.profile = self.root / 'etc/fxroute-iso-profile'
        self.profile.parent.mkdir()
        self.profile.write_text('headless\n')
        self.env = dict(os.environ, TEST_ROOT=str(self.root))

    def run_script(self, name):
        path = SCRIPTS / name
        self.assertTrue(path.exists(), f'Missing implementation: {path.name}')
        source = path.read_text()
        # Redirect absolute target paths, not production logic. Replace PATH
        # last so our fake tools take precedence even in the first-boot script.
        for prefix in ('/etc/', '/opt/', '/var/lib/', '/usr/local/', '/usr/share/'):
            source = source.replace(prefix, str(self.root) + prefix)
        source = '\n'.join(line for line in source.splitlines()
                           if not line.startswith('PATH='))
        prelude = f'export PATH={shlex.quote(str(self.bin))}:/usr/bin:/bin\n'
        # Model a system with no browser, independently of host packages.
        prelude += 'command() { if [[ "$*" == "-v firefox" ]]; then return 1; fi; builtin command "$@"; }\n'
        return subprocess.run(['bash', '-c', prelude + source], env=self.env,
                              text=True, capture_output=True, timeout=15)

    def calls(self, name=None):
        log = self.root / 'commands'
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call for call in calls if name is None or call[0] == name]

    def seed_firefox(self):
        seed = self.root / 'var/lib/snapd/seed'
        (seed / 'snaps').mkdir(parents=True)
        (seed / 'seed.yaml').write_text('snaps:\n- name: firefox\n  file: firefox_123.snap\n- name: core24\n  file: core24_456.snap\n')
        (seed / 'snaps/firefox_123.snap').touch()
        (seed / 'snaps/core24_456.snap').touch()
        return seed

    def installed_firefox(self):
        state = self.root / 'var/lib/snapd/state.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'data': {'snaps': {
            'firefox': {'active': True, 'sequence': [{'revision': '123'}]},
            'core24': {'active': True}}}}))
        return state


class HeadlessTargetTest(IsolatedScripts):
    def test_explicit_purge_preserves_network_audio_and_sets_boot_target(self):
        desktop = ['ubuntu-desktop', 'ubuntu-desktop-minimal', 'gdm3', 'gnome-shell',
                   'gnome-session', 'ubuntu-session', 'firefox']
        retained = ['network-manager', 'wpasupplicant', 'netplan.io', 'linux-firmware',
                    'pipewire', 'pipewire-pulse', 'wireplumber', 'bluez', 'openssh-server',
                    'gnome-keyring', 'libpam-gnome-keyring']
        self.packages.write_text(json.dumps(dict.fromkeys(desktop + retained, 'installed')))
        result = self.run_script('prepare-headless-target.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(self.packages.read_text())), set(retained))
        apt_calls = self.calls('apt-get')
        self.assertEqual(len(apt_calls), 2)
        self.assertIn('--simulate', apt_calls[0])
        self.assertNotIn('--simulate', apt_calls[1])
        for call in apt_calls:
            self.assertEqual(set(call[call.index('purge') + 1:]), set(desktop))
            self.assertNotIn('autoremove', call)
            self.assertIn('APT::Get::AutomaticRemove=false', call)
        self.assertIn(['systemctl', 'set-default', 'multi-user.target'], self.calls())
        self.assertFalse(any('--now' in call for call in self.calls('systemctl')))

    def test_rejects_collateral_removal_before_mutating_packages_or_snaps(self):
        for package in ('network-manager', 'wpasupplicant', 'pipewire:amd64',
                        'wireplumber', 'openssh-server', 'unanticipated-package'):
            with self.subTest(package=package):
                self.packages.write_text('{"gdm3": "installed"}')
                self.env['EXTRA_REMOVALS'] = json.dumps([package])
                result = self.run_script('prepare-headless-target.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(package, result.stderr)
                self.assertEqual(json.loads(self.packages.read_text()), {'gdm3': 'installed'})
                self.assertFalse(self.calls('snap'))
                self.assertTrue(all('--simulate' in call for call in self.calls('apt-get')))

    def test_failed_simulation_aborts(self):
        self.packages.write_text('{"gdm3": "installed"}')
        self.env['APT_SIMULATE_STATUS'] = '100'
        result = self.run_script('prepare-headless-target.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls('apt-get')), 1)

    def test_purge_failure_or_remaining_packages_prevent_success(self):
        for failure in ('APT_PURGE_FAIL', 'APT_LEAVES_PACKAGES'):
            with self.subTest(failure=failure):
                self.packages.write_text('{"gdm3": "installed"}')
                self.env[failure] = '1'
                result = self.run_script('prepare-headless-target.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls('systemctl'))
                del self.env[failure]

    def test_config_only_and_multiarch_desktop_packages_are_purged(self):
        self.packages.write_text('{"gdm3": "config-files", "gnome-shell:amd64": "installed"}')
        result = self.run_script('prepare-headless-target.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.packages.read_text()), {})

    def test_no_desktop_packages_is_idempotent_without_apt(self):
        for _ in range(2):
            result = self.run_script('prepare-headless-target.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.calls('apt-get'))

    def test_unseeded_firefox_removed_offline_without_touching_other_snaps(self):
        seed = self.seed_firefox()
        for _ in range(2):
            result = self.run_script('prepare-headless-target.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('firefox', (seed / 'seed.yaml').read_text())
        self.assertFalse((seed / 'snaps/firefox_123.snap').exists())
        self.assertTrue((seed / 'snaps/core24_456.snap').exists())
        self.assertFalse(self.calls('snap'), 'Unseeded target has no running snap daemon')

    def test_installed_firefox_removed_using_snap_and_seed_pruned(self):
        seed = self.seed_firefox()
        state = self.installed_firefox()
        result = self.run_script('prepare-headless-target.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('firefox', json.loads(state.read_text())['data']['snaps'])
        self.assertIn('core24', json.loads(state.read_text())['data']['snaps'])
        self.assertNotIn('firefox', (seed / 'seed.yaml').read_text())
        self.assertEqual(self.calls('snap'), [['snap', 'remove', '--purge', 'firefox']])

    def test_snap_failure_is_not_silently_accepted(self):
        self.installed_firefox()
        self.env['SNAP_REMOVE_FAIL'] = '1'
        result = self.run_script('prepare-headless-target.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls('systemctl'))

    def test_helper_refuses_desktop_missing_and_invalid_profiles(self):
        for profile in ('desktop', '', 'headles', None):
            with self.subTest(profile=profile):
                if profile is None:
                    self.profile.unlink()
                else:
                    self.profile.write_text(profile)
                result = self.run_script('prepare-headless-target.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls('apt-get'))
                self.assertFalse(self.calls('systemctl'))


class FirstBootProfileTest(IsolatedScripts):
    def setUp(self):
        super().setUp()
        archive = self.root / 'opt/fxroute-iso/source.tar'
        archive.parent.mkdir(parents=True)
        archive.touch()
        (self.root / 'home/fxroute').mkdir(parents=True)

    def test_headless_reuses_installer_ssh_and_lan_without_browser(self):
        result = self.run_script('first-boot-install-ubuntu.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'var/lib/fxroute-iso/install-complete').exists())
        args = (self.root / 'installer-args').read_text().split()
        for option in ('--with-lan-name', '--with-caddy', '--user', '--providers', '--yes'):
            self.assertIn(option, args)
        self.assertIn(['systemctl', 'enable', '--now', 'ssh.service'], self.calls())
        self.assertFalse((self.root / 'etc/dconf').exists())
        self.assertFalse((self.root / 'etc/firefox').exists())
        self.assertFalse((self.root / 'home/fxroute/.config/autostart').exists())
        self.assertFalse(any('gdm' in ' '.join(call) or 'graphical.target' in call
                             for call in self.calls('systemctl')))

    def test_desktop_and_missing_profile_keep_browser_requirement(self):
        for profile in ('desktop', None):
            with self.subTest(profile=profile):
                if profile is None:
                    self.profile.unlink()
                else:
                    self.profile.write_text(profile)
                result = self.run_script('first-boot-install-ubuntu.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('firefox is missing', result.stderr)
                self.assertFalse((self.root / 'var/lib/fxroute-iso/install-complete').exists())

    def test_invalid_profile_fails_before_installer_or_ssh(self):
        for profile in ('', 'headles', 'headless\ndesktop'):
            with self.subTest(profile=profile):
                self.profile.write_text(profile)
                result = self.run_script('first-boot-install-ubuntu.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unknown FXRoute ISO profile', result.stderr)
                self.assertFalse((self.root / 'installer-args').exists())
                self.assertFalse(self.calls('systemctl'))


if __name__ == '__main__':
    unittest.main()
