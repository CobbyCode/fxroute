#!/usr/bin/env python3
"""Run ISO harness checks without contacting SSH or starting QEMU."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('test-ubuntu-iso.sh')


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fxroute-harness-', dir='/tmp/opencode')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, FXROUTE_UBUNTU_TEST_DIR=str(self.root),
                        FXROUTE_UBUNTU_TEST_PASSWORD='test password')

    def run_bash(self, body, before='', **env):
        safety = '''
qemu-system-x86_64() { echo 'Unexpected QEMU invocation' >&2; return 99; }
qemu-img() { echo 'Unexpected disk creation' >&2; return 99; }
ssh() { echo 'Unexpected SSH invocation' >&2; return 99; }
kill() { echo 'Unexpected signal' >&2; return 99; }
'''
        return subprocess.run(
            ['bash', '-c', safety + before + '\nsource ' + shlex.quote(str(SCRIPT)) + '\n' + body],
            env=dict(self.env, **env), text=True, capture_output=True, timeout=10)

    def test_source_has_no_preflight_or_vm_side_effects(self):
        result = self.run_bash('declare -F ssh_run >/dev/null')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_both_ssh_users_use_askpass_and_wait_for_child_status(self):
        mock = r'''
setsid() {
  [[ "$1" == -w ]] || return 91
  shift
  "$@"
}
ssh() {
  [[ "$SSH_ASKPASS" == */ubuntu/test-askpass.sh && -x "$SSH_ASKPASS" ]] || return 92
  [[ "$SSH_ASKPASS_REQUIRE" == force && "$DISPLAY" == :0 ]] || return 93
  [[ "$FXROUTE_ASKPASS_PASSWORD" == 'test password' ]] || return 94
  [[ "$*" == *PreferredAuthentications=password* && "$*" == *PubkeyAuthentication=no* ]] || return 95
  [[ "$*" == *"$EXPECTED_USER@127.0.0.1"* ]] || return 96
  return 37
}
'''
        for helper, user in [('ssh_run', 'test'), ('live_ssh_run', 'ubuntu')]:
            with self.subTest(helper=helper):
                result = self.run_bash(mock + f'\n{helper} true', EXPECTED_USER=user)
                self.assertEqual(result.returncode, 37, result.stderr)

    def guest_fixture(self):
        (self.root / 'marker').touch()
        (self.root / 'cmdline').write_text('root=/dev/mapper/ubuntu--vg-ubuntu--lv ro quiet\n')
        return r'''
ssh_run() {
  local command="$1"
  command="${command//\/proc\/cmdline/$TEST_ROOT/cmdline}"
  command="${command//\/var\/lib\/fxroute-iso\/install-complete/$TEST_ROOT/marker}"
  bash -c "$command"
}
findmnt() {
  if [[ "$*" == *FSTYPE* ]]; then echo "${ROOT_TYPE-ext4}";
  else echo /dev/mapper/ubuntu--vg-ubuntu--lv; fi
}
systemctl() { [[ "$*" != *"${FAILED_SERVICE:-never.service}"* ]]; }
loginctl() {
  case "$*" in
    *ActiveSession*) echo 2 ;;
    *Name*) echo "${SEAT_USER:-test}" ;;
    *Service*) echo "${SESSION_SERVICE:-gdm-autologin}" ;;
    *Type*) echo wayland ;;
    *Active*) echo yes ;;
    *) return 1 ;;
  esac
}
pgrep() {
  [[ "$*" == *firefox* || "$*" == *'[f]irefox'* ]] && [[ "$*" == *--kiosk* ]] || return 98
  [[ "${NO_KIOSK:-0}" == 0 ]]
}
gsettings() {
  case "$*" in
    *idle-delay*) echo "uint32 ${IDLE_DELAY:-0}" ;;
    *lock-enabled*) echo "${LOCK_ENABLED:-false}" ;;
    *) return 1 ;;
  esac
}
export -f findmnt systemctl loginctl pgrep gsettings
export TEST_ROOT
'''

    def test_installed_os_rejects_live_root_cmdline_and_missing_marker(self):
        fixture = self.guest_fixture()
        for kind in ['installed', 'overlay', 'empty-root', 'casper', 'autoinstall', 'missing-marker']:
            with self.subTest(kind=kind):
                (self.root / 'marker').touch()
                (self.root / 'cmdline').write_text(
                    {'casper': 'boot=casper', 'autoinstall': 'autoinstall'}.get(kind, 'root=/dev/mapper/vg-root ro'))
                if kind == 'missing-marker':
                    (self.root / 'marker').unlink()
                result = self.run_bash(fixture + '\ncheck_installed_os',
                                       ROOT_TYPE={'overlay': 'overlay', 'empty-root': ''}.get(kind, 'ext4'))
                self.assertEqual(result.returncode == 0, kind == 'installed', result.stderr)

    def test_appliance_checks_require_desktop_and_each_service(self):
        fixture = self.guest_fixture()
        variants = [{}, {'SEAT_USER': 'gdm'}, {'SESSION_SERVICE': 'gdm-password'},
                    {'NO_KIOSK': '1'}, {'IDLE_DELAY': '300'}, {'LOCK_ENABLED': 'true'}]
        variants += [{'FAILED_SERVICE': service} for service in
                     ['fxroute.service', 'pipewire.service', 'wireplumber.service', 'display-manager.service']]
        for variant in variants:
            with self.subTest(variant=variant):
                result = self.run_bash(fixture + '\ncheck_appliance', **variant)
                self.assertEqual(result.returncode == 0, not variant, result.stderr)

    def test_wait_retries_failures_and_times_out(self):
        result = self.run_bash('''
sleep() { :; }
attempts=0
probe() { attempts=$((attempts + 1)); (( attempts >= 3 )); }
wait_for_check 30 probe
[[ "$attempts" == 3 ]]
if wait_for_check 20 false; then exit 97; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_wait_retries_and_propagates_timeout(self):
        result = self.run_bash('''
sleep() { :; }
attempts=0
curl() {
  [[ "$*" == *--fail* && "$*" == *--max-time* ]] || exit 98
  attempts=$((attempts + 1))
  (( attempts >= 3 ))
}
wait_for_http http://example.invalid/api/status 30
[[ "$attempts" == 3 ]]
curl() { return 22; }
if wait_for_http http://example.invalid/api/status 20; then exit 97; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_appliance_failure_leaves_guest_for_diagnosis(self):
        for name in ['install.qcow2', 'ovmf-vars.fd']:
            (self.root / name).write_text('keep me')
        for failing_stage in ['http', 'checks']:
            with self.subTest(stage=failing_stage):
                result = self.run_bash('''
boot_disk() { :; }
wait_for_http() { [[ "$FAILING_STAGE" != http ]]; }
wait_for_check() { [[ "$FAILING_STAGE" != checks ]]; }
shutdown_guest() { echo 'SHUTDOWN CALLED'; }
phase_appliance
''', FAILING_STAGE=failing_stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('SHUTDOWN CALLED', result.stdout)
                self.assertNotIn('appliance checks passed', result.stdout)

    def test_install_detects_installed_os_then_shuts_down_before_disk_boot(self):
        result = self.run_bash('''
qemu-img() { :; }
boot_iso() { echo iso; }
wait_for_live_ssh() { return 0; }
live_ssh_run() { return 0; }
wait_for_check() { [[ "$*" == *check_installed_os* ]]; echo installed; }
shutdown_guest() { [[ "$1" == */install-monitor.sock && "$2" == */install.pid ]]; echo shutdown; }
phase_appliance() { echo appliance; }
sleep() { :; }
phase_install
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [line for line in result.stdout.splitlines() if not line.startswith('[ubuntu-test]')]
        self.assertEqual(events, ['iso', 'installed', 'shutdown', 'appliance'])

    def test_shutdown_waits_for_exit_and_never_forces_guest(self):
        (self.root / 'install.pid').write_text('12345\n')
        result = self.run_bash('''
qemu_monitor() { [[ "$2" == system_powerdown ]]; }
sleep() { :; }
polls=0
kill() { [[ "$1" == -0 && "$2" == 12345 ]] || exit 98; polls=$((polls + 1)); (( polls < 3 )); }
shutdown_guest "$TEST_ROOT/install-monitor.sock" "$TEST_ROOT/install.pid"
[[ "$polls" == 3 ]]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_bash('''
qemu_monitor() { :; }
sleep() { :; }
kill() { [[ "$1" == -0 ]] || exit 98; return 0; }
shutdown_guest "$TEST_ROOT/install-monitor.sock" "$TEST_ROOT/install.pid"
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 98)

    def test_appliance_mode_preserves_existing_disk_and_vars_without_iso(self):
        for name in ['install.qcow2', 'ovmf-vars.fd', 'code.fd']:
            (self.root / name).write_text('keep me')
        result = self.run_bash('''
MODE=appliance
ISO="$TEST_ROOT/no.iso"
OVMF_CODE="$TEST_ROOT/code.fd"
qemu-system-x86_64() {
  [[ "$*" == *install.qcow2* && "$*" == *ovmf-vars.fd* && "$*" != *-cdrom* ]] || return 96
  echo disk-boot
}
wait_for_http() { echo http; }
wait_for_check() { [[ "$*" == *check_appliance* ]]; echo checks; }
shutdown_guest() { echo shutdown; }
main
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ['install.qcow2', 'ovmf-vars.fd']:
            self.assertEqual((self.root / name).read_text(), 'keep me')
        events = [line for line in result.stdout.splitlines() if not line.startswith('[ubuntu-test]')]
        self.assertEqual(events, ['disk-boot', 'http', 'checks', 'shutdown'])

    def test_appliance_refuses_missing_inputs_or_running_install_guest(self):
        for name in ['install.qcow2', 'ovmf-vars.fd', 'code.fd']:
            (self.root / name).write_text('keep me')
        for kind in ['disk', 'vars', 'running']:
            with self.subTest(kind=kind):
                body = '''
MODE=appliance
OVMF_CODE="$TEST_ROOT/code.fd"
kill() { [[ "$1" == -0 ]]; }
qemu-system-x86_64() { echo 'BOOT CALLED'; return 98; }
'''
                if kind == 'running':
                    body += 'echo 12345 > "$TEST_ROOT/install.pid"\n'
                else:
                    name = 'install.qcow2' if kind == 'disk' else 'ovmf-vars.fd'
                    body += f'rm "$TEST_ROOT/{name}"\n'
                result = self.run_bash(body + 'main')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('BOOT CALLED', result.stdout)
                for name in ['install.qcow2', 'ovmf-vars.fd']:
                    (self.root / name).write_text('keep me')


if __name__ == '__main__':
    unittest.main()
