#!/usr/bin/env python3
"""Exercise the live-session autostart with fake system tools (no root, no network)."""
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parent / 'scripts' / 'fxroute-live-autostart.sh'

# No package, service or network operation may touch the test host. The fake
# tools are state-driven through $TEST_ROOT/state-* files.
FAKE_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'commands').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
def state(key, default=None):
    path = root / ('state-' + key)
    return path.read_text() if path.exists() else default
if name == 'curl':
    url = args[-1] if args else ''
    if 'api/status' in url:
        sys.exit(0 if state('backend', 'down') == 'up' else 1)
    sys.exit(0 if state('network', 'up') == 'up' else 1)
elif name == 'nm-online':
    sys.exit(0 if state('network', 'up') == 'up' and not os.environ.get('NM_FAIL') else 1)
elif name == 'fuser':
    sys.exit(0 if state('apt-locked', 'no') == 'yes' else 1)
elif name == 'sudo':
    inner = [a for a in args if a != '-n']
    if inner[:2] == ['apt-get', 'update']:
        count = int(state('apt-updates', '0')) + 1
        (root / 'state-apt-updates').write_text(str(count))
        sys.exit(1 if count <= int(os.environ.get('APT_UPDATE_FAILS', '0')) else 0)
    sys.exit(0)
elif name == 'systemctl':
    sys.exit(1 if state('service', 'ok') == 'fail' else 0)
elif name == 'firefox':
    (root / 'firefox-opened').write_text('yes')
    sys.exit(0)
else:
    raise RuntimeError('Unexpected tool: ' + name)
'''

INSTALL_FIXTURE = r'''#!/usr/bin/env bash
echo "fixture install.sh $*" >> "$TEST_ROOT/installer-log"
[[ "${INSTALL_FAIL:-0}" == "1" ]] && exit 1
mkdir -p "$HOME/fxroute"
touch "$HOME/fxroute/main.py"
'''


class LiveAutostartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fxroute-live-')
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('curl', 'nm-online', 'fuser', 'sudo', 'systemctl', 'firefox'):
            tool = self.bin / name
            tool.write_text(FAKE_TOOL)
            tool.chmod(0o755)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        fixture = self.source / 'install.sh'
        fixture.write_text(INSTALL_FIXTURE)
        fixture.chmod(0o755)
        (self.root / 'state-network').write_text('up')
        (self.root / 'state-backend').write_text('up')
        self.env = dict(os.environ, TEST_ROOT=str(self.root), HOME=str(self.home),
                        FXROUTE_LIVE_FORCE='1', FXROUTE_LIVE_SOURCE_DIR=str(self.source),
                        FXROUTE_LIVE_APT_RETRY_DELAY='0',
                        PATH=str(self.bin) + ':/usr/bin:/bin')

    def run_script(self):
        self.assertTrue(SCRIPT.exists(), 'Missing implementation')
        return subprocess.run(['bash', str(SCRIPT)], env=self.env,
                              text=True, capture_output=True, timeout=120)

    def hint(self):
        hint = self.home / 'Desktop' / 'FXRoute-NOT-STARTED.txt'
        return hint.read_text() if hint.exists() else None

    def test_success_marks_ready_and_opens_kiosk(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/share/fxroute/live-ready').exists())
        self.assertTrue((self.root / 'firefox-opened').exists())
        self.assertIsNone(self.hint())

    def test_install_failure_leaves_no_marker_and_opens_no_kiosk(self):
        self.env['INSTALL_FAIL'] = '1'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local/share/fxroute/live-ready').exists())
        self.assertIsNotNone(self.hint())
        self.assertFalse((self.root / 'firefox-opened').exists())

    def test_service_start_failure_leaves_no_marker(self):
        (self.root / 'state-service').write_text('fail')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local/share/fxroute/live-ready').exists())
        self.assertIsNotNone(self.hint())
        self.assertFalse((self.root / 'firefox-opened').exists())

    def test_no_network_aborts_before_install(self):
        (self.root / 'state-network').write_text('down')
        self.env['FXROUTE_LIVE_NETWORK_TIMEOUT'] = '6'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'installer-log').exists())
        self.assertFalse((self.home / '.local/share/fxroute/live-ready').exists())
        self.assertIsNotNone(self.hint())

    def test_apt_update_is_retried_until_success(self):
        self.env['APT_UPDATE_FAILS'] = '2'
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'state-apt-updates').read_text(), '3')
        self.assertTrue((self.root / 'firefox-opened').exists())

    def test_persistent_apt_failure_aborts_before_install(self):
        self.env['APT_UPDATE_FAILS'] = '99'
        self.env['FXROUTE_LIVE_APT_RETRIES'] = '2'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'installer-log').exists())
        self.assertFalse((self.home / '.local/share/fxroute/live-ready').exists())
        self.assertIsNotNone(self.hint())

    def test_backend_timeout_leaves_hint(self):
        (self.root / 'state-backend').write_text('down')
        self.env['FXROUTE_LIVE_BACKEND_TIMEOUT'] = '3'
        result = self.run_script()
        self.assertIsNotNone(self.hint())


if __name__ == '__main__':
    unittest.main()
