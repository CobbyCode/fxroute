#!/usr/bin/env python3
"""Installer/first-boot completion contracts, with no host installation actions."""

import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_installer_integrations import extract_function


class InstallerCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Path(self.temporary.name)
        self.install = (ROOT / "install.sh").read_text()
        self.installer = self.sandbox / "installer"
        self.installer.write_text(self._installer_harness())
        self.installer.chmod(0o755)
        (self.sandbox / ".env").write_text("PORT=8000\n")

    def _installer_harness(self):
        # Extract production entry/gate/summary only. Every installation action
        # and external probe is replaced; this never sources/runs install.sh.
        main = extract_function(self.install, "main")
        stubs = set(re.findall(r"^  ([a-z][a-z_]+)(?: |$)", main, re.MULTILINE))
        stubs -= {"if", "fi", "return", "trap", "validate_http", "print_summary"}
        stub_functions = "\n".join(name + '() { :; }' for name in sorted(stubs))
        helpers = "\n".join(
            next(line for line in self.install.splitlines() if line.startswith(name + "()"))
            for name in ("pass", "fail", "warn", "die")
        )
        return f"""#!/usr/bin/env bash
set -Eeuo pipefail
{stub_functions}
bash() {{ :; }}
python3() {{ :; }}
log() {{ :; }}
{helpers}
{extract_function(self.install, 'validate_http')}
{extract_function(self.install, 'print_summary')}
{extract_function(self.install, 'checkpoint_install_state_on_exit')}
{main}
INSTALL_ROOT={shlex.quote(str(self.sandbox))}
HOME="$INSTALL_ROOT"
FXROUTE_TARGET_USER=testuser
PROVIDERS_ONLY_MODE=0
SUDO_CMD=()
SERVICE_NAME=fxroute
HTTP_VALIDATION_FAILED=0
VALIDATION_RESULTS=()
WARNINGS=()
SPOTIFY_DESKTOP_PROVIDER_STATUS=disabled
SPOTIFYD_PROVIDER_STATUS=disabled
QOBUZ_PROVIDER_STATUS=disabled
TIDAL_PROVIDER_STATUS=disabled
CADDY_PROXY_ENABLED=0
MDNS_GUARD_ENABLED=0
FXROUTE_HTTP_VALIDATION_DEADLINE=0
primary_lan_ip() {{ :; }}
ensure_lan_firewall_service_open() {{ :; }}
record() {{ printf '%s\\n' "$*" >> "$INSTALL_ROOT/trace"; }}
cleanup_active_temp_dir() {{ record "checkpoint:$exit_status"; }}
write_install_config() {{ record write-config; }}
finalize_firewalld_rule_format() {{ record finalize-firewall; }}
write_install_state() {{ record write-state; }}
ensure_target_user_ownership() {{ record ownership-checked; }}
user_systemctl() {{
  case "$*" in
    *MainPID*) printf '123\\n' ;;
    *ActiveState*) printf '%s\\n' "${{SERVICE_STATE:-active}}" ;;
  esac
}}
ss() {{ [[ "${{PORT_OWNED:-1}}" == 0 ]] || printf 'LISTEN :8000 pid=123,\\n'; }}
curl() {{ return "${{HEALTH_STATUS:-1}}"; }}
validate_tools() {{
  if [[ "${{OPTIONAL_FAILURE:-0}}" == 1 ]]; then fail 'bluetoothctl available'; fi
}}
validate_pipewire_session() {{
  printf 'session-gate\\n'
  record session-gate
  if [[ "${{SESSION_FAILURE:-0}}" == 1 ]]; then die 'session failed'; fi
}}
main
"""

    def _run(self, script=None, **settings):
        return subprocess.run(
            ["bash", str(script or self.installer)],
            env={**os.environ, **{key: str(value) for key, value in settings.items()}},
            capture_output=True, text=True, timeout=10,
        )

    def test_unhealthy_http_fails_after_session_gate_and_bookkeeping(self):
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[fail] HTTP health response", result.stdout)
        self.assertIn("FXRoute installation failed", result.stdout)
        self.assertIn("session-gate", result.stdout)
        trace = (self.sandbox / "trace").read_text().splitlines()
        self.assertEqual(trace[trace.index("session-gate"):], [
            "session-gate", "write-config", "finalize-firewall", "write-state", "ownership-checked",
            "checkpoint:1", "write-config", "write-state", "ownership-checked",
        ])
        self.assertNotIn("FXRoute installed with warnings", result.stdout)

    def test_inactive_service_and_wrong_listener_fail_completion(self):
        for settings in ({"SERVICE_STATE": "inactive"}, {"PORT_OWNED": 0}):
            with self.subTest(settings=settings):
                result = self._run(**settings)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("FXRoute installation failed", result.stdout)

    def test_healthy_http_succeeds(self):
        result = self._run(HEALTH_STATUS=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FXRoute installed successfully", result.stdout)

    def test_optional_tool_failure_is_still_nonfatal(self):
        result = self._run(HEALTH_STATUS=0, OPTIONAL_FAILURE=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FXRoute installed with warnings", result.stdout)

    def test_fatal_session_gate_still_prevents_completion(self):
        result = self._run(HEALTH_STATUS=0, SESSION_FAILURE=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session failed", result.stderr)
        self.assertNotIn("FXRoute installed successfully", result.stdout)

    def _first_boot_harness(self, platform):
        text = (ROOT / platform).read_text()
        platform_root = self.sandbox / platform.split("/")[0]
        platform_root.mkdir()
        state = platform_root / "state"
        state.mkdir()
        source = platform_root / "source"
        source.mkdir()
        archive = source / "source.tar"
        archive.write_text("retained installation source")
        helpers = extract_function(text, "cleanup")
        if platform.startswith("armbian/"):
            helpers += "\n" + extract_function(text, "write_durable_marker")
            cleanup = extract_function(text, "finish_success_cleanup")
            # Keep the real cleanup behavior but redirect every absolute path
            # to this test's sandbox (including the success case on old code).
            for original in ("/root/.not_logged_in_yet", "/etc/profile.d/armbian-check-first-login.sh",
                             "/var/lib/armbian-web-config", "/opt/fxroute-armbian"):
                cleanup = cleanup.replace(original, str(source if original.startswith("/opt/")
                                                        else self.sandbox / original.lstrip("/")))
            helpers += "\n" + cleanup
        script = self.sandbox / "first-boot"
        script.write_text(f"""set -Eeuo pipefail
STATE_DIR={shlex.quote(str(state))}
COMPLETE_MARKER="$STATE_DIR/install-complete"
FAILED_MARKER="$STATE_DIR/install-failed"
IN_PROGRESS_MARKER="$STATE_DIR/install-in-progress"
CLEANUP_PENDING_MARKER="$STATE_DIR/install-cleanup-pending"
SERVICE_NAME=test-first-boot
staging_dir=''
completed=0
systemctl() {{ :; }}
{helpers}
trap cleanup EXIT
{shlex.quote(str(self.installer))}
completed=1
printf 'FXRoute ready\\n'
""")
        return script, state, archive

    def test_failed_http_does_not_complete_any_first_boot(self):
        for platform in ("armbian/first-boot-install.sh", "iso/scripts/first-boot-install.sh",
                         "ubuntu/scripts/first-boot-install-ubuntu.sh"):
            with self.subTest(platform=platform):
                script, state, archive = self._first_boot_harness(platform)
                result = self._run(script)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("HTTP health response", result.stdout)
                self.assertTrue((state / "install-failed").is_file())
                self.assertFalse((state / "install-complete").exists())
                self.assertFalse((state / "install-cleanup-pending").exists())
                self.assertTrue(archive.is_file())
                self.assertNotIn("FXRoute ready", result.stdout)

    def test_successful_http_allows_first_boot_completion(self):
        script, state, archive = self._first_boot_harness("armbian/first-boot-install.sh")
        result = self._run(script, HEALTH_STATUS=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((state / "install-complete").is_file())
        self.assertFalse((state / "install-failed").exists())
        self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main()
