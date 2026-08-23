#!/usr/bin/env python3
"""Regression tests for installer/provider integration contracts."""

import re
import os
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


class InstallerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()

    def test_qobuz_volume_configuration_sets_locked_without_replacing_existing_settings(self):
        for name in (
            "read_qbzd_volume_mode",
            "set_qbzd_volume_mode",
            "configure_qbzd_volume_mode",
        ):
            self.assertIn(f"{name}()", self.install)
        self.assertIn("qconnect.volume_mode", self.install)
        self.assertIn("QOBUZ_VOLUME_MODE_KEY", self.install)
        self.assertIn("QOBUZ_REQUIRED_VOLUME_MODE", self.install)
        self.assertIn("volume_mode_changed_by_fxroute", self.install)

        reader = extract_function(self.install, "read_qbzd_volume_mode")
        setter = extract_function(self.install, "set_qbzd_volume_mode")
        configurator = extract_function(self.install, "configure_qbzd_volume_mode")
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "mode"
            log = Path(td) / "calls"
            fake_qbzd = Path(td) / "qbzd"
            fake_qbzd.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == settings && $2 == show ]]; then\n"
                "  printf '{\"qconnect.volume_mode\":\"%s\",\"qconnect.device_name\":\"keep\"}\\n' \"$(<\"$MODE_FILE\")\"\n"
                "elif [[ $1 == settings && $2 == set ]]; then\n"
                "  if [[ $3 == --quiet ]]; then key=$4; value=$5; else key=$3; value=$4; fi\n"
                "  printf '%s %s\\n' \"$key\" \"$value\" >> \"$CALL_LOG\"\n"
                "  printf '%s' \"$value\" > \"$MODE_FILE\"\n"
                "fi\n"
            )
            fake_qbzd.chmod(0o755)
            state.write_text("software")
            harness = f"""
QBZD_BINARY_PATH={fake_qbzd}
MODE_FILE={state}
CALL_LOG={log}
QOBUZ_VOLUME_MODE_KEY=qconnect.volume_mode
QOBUZ_REQUIRED_VOLUME_MODE=locked
export MODE_FILE CALL_LOG
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_VOLUME_MODE_BEFORE=""
qbzd_binary_path() {{ printf '%s\\n' "$QBZD_BINARY_PATH"; }}
run_cmd() {{ "$@"; }}
pass() {{ :; }}
warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
die() {{ printf 'DIE:%s\\n' "$*" >&2; return 1; }}
{reader}
{setter}
{configurator}
configure_qbzd_volume_mode
printf 'mode=%s changed=%s before=%s\\n' "$(<"$MODE_FILE")" "$QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE" "$QBZD_VOLUME_MODE_BEFORE"
"""
            result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=locked changed=1 before=software", result.stdout)
            self.assertEqual(log.read_text().strip(), "qconnect.volume_mode locked")

            state.write_text("locked")
            log.unlink()
            result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=locked changed=0 before=", result.stdout)
            self.assertFalse(log.exists())

            state.write_text("hardware")
            if log.exists():
                log.unlink()
            rerun_harness = harness.replace(
                "QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0",
                "QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=1",
            ).replace(
                'QBZD_VOLUME_MODE_BEFORE=""',
                'QBZD_VOLUME_MODE_BEFORE=software',
            )
            result = subprocess.run(["bash", "-c", rerun_harness], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=locked changed=1 before=hardware", result.stdout)
            self.assertEqual(log.read_text().strip(), "qconnect.volume_mode locked")

    def test_qobuz_volume_ownership_is_persisted_and_uninstaller_handles_it(self):
        for field in (
            "volume_mode_before",
            "volume_mode_after",
            "volume_mode_changed_by_fxroute",
        ):
            self.assertIn(field, self.install)
            self.assertIn(field, self.uninstall)
        self.assertIn("qconnect.volume_mode", self.uninstall)
        configurator = extract_function(self.install, "configure_qbzd_volume_mode")
        self.assertLess(
            configurator.index("QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=1"),
            configurator.index("set_qbzd_volume_mode"),
        )

    def test_qobuz_owned_binary_hash_is_not_replaced_after_external_change(self):
        path_reader = extract_function(self.install, "qbzd_binary_path")
        installer = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            binary_path = home / ".local" / "bin" / "qbzd"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_text("replacement")
            expected_sha256 = "original-sha256"
            harness = f"""
{path_reader}
{installer}
QBZD_INSTALLED_BY_FXROUTE=1
QBZD_BINARY_SHA256={expected_sha256}
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_PRESENT_BEFORE=0
pass() {{ :; }}
warn() {{ :; }}
install_qbzd_binary
printf 'hash=%s changed=%s\\n' "$QBZD_BINARY_SHA256" "$QBZD_BINARY_IDENTITY_CHANGED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"hash={expected_sha256} changed=1", result.stdout)
            missing_hash_result = subprocess.run(
                ["bash", "-c", harness.replace(f"QBZD_BINARY_SHA256={expected_sha256}", "QBZD_BINARY_SHA256=''")],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(missing_hash_result.returncode, 0, missing_hash_result.stderr)
            self.assertIn("hash= changed=1", missing_hash_result.stdout)
            volume_owned_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness.replace(
                        "QBZD_INSTALLED_BY_FXROUTE=1",
                        "QBZD_INSTALLED_BY_FXROUTE=0\nQBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=1",
                    ),
                ],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(volume_owned_result.returncode, 0, volume_owned_result.stderr)
            self.assertIn(f"hash={expected_sha256} changed=1", volume_owned_result.stdout)

    def test_spotifyd_owned_binary_hash_is_not_replaced_after_external_change(self):
        path_reader = extract_function(self.install, "spotifyd_binary_path")
        installer = extract_function(self.install, "install_spotifyd_binary")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            binary_path = home / ".local" / "bin" / "spotifyd"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_text("replacement")
            expected_sha256 = "original-sha256"
            harness = f"""
{path_reader}
{installer}
SPOTIFYD_INSTALLED_BY_FXROUTE=1
SPOTIFYD_BINARY_PATH="$HOME/.local/bin/spotifyd"
SPOTIFYD_BINARY_SHA256={expected_sha256}
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_PRESENT_BEFORE=0
pass() {{ :; }}
warn() {{ :; }}
install_spotifyd_binary
printf 'hash=%s changed=%s\\n' "$SPOTIFYD_BINARY_SHA256" "$SPOTIFYD_BINARY_IDENTITY_CHANGED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"hash={expected_sha256} changed=1", result.stdout)

    def test_uninstaller_restores_fxroute_owned_qobuz_volume_mode(self):
        reader = extract_function(self.uninstall, "read_qbzd_volume_mode_for_uninstall")
        restore = extract_function(self.uninstall, "restore_qbzd_volume_mode_if_owned")
        with tempfile.TemporaryDirectory() as td:
            mode_file = Path(td) / "mode"
            install_state = Path(td) / "install-state.json"
            fake_qbzd = Path(td) / "qbzd"
            mode_file.write_text("locked")
            install_state.write_text(
                '{"providers":{"qobuz":{"volume_mode_before":"software",'
                '"volume_mode_after":"locked",'
                '"volume_mode_changed_by_fxroute":true}}}\n'
            )
            fake_qbzd.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == settings && $2 == show ]]; then\n"
                "  printf '{\"qconnect.volume_mode\":\"%s\"}\\n' \"$(<\"$MODE_FILE\")\"\n"
                "elif [[ $1 == settings && $2 == set ]]; then\n"
                "  printf '%s' \"${5:-$4}\" > \"$MODE_FILE\"\n"
                "fi\n"
            )
            fake_qbzd.chmod(0o755)
            qbzd_sha256 = hashlib.sha256(fake_qbzd.read_bytes()).hexdigest()
            harness = f"""
{reader}
{extract_function(self.uninstall, "clear_qbzd_volume_ownership_record")}
{extract_function(self.uninstall, "verify_owned_binary_identity")}
{restore}
read_install_state_field() {{
  case "$1" in
    providers.qobuz.volume_mode_changed_by_fxroute) printf 'true\\n' ;;
    providers.qobuz.volume_mode_before) printf 'software\\n' ;;
    providers.qobuz.volume_mode_after) printf 'locked\\n' ;;
    providers.qobuz.binary_path) printf '%s\\n' "$QBZD_BINARY" ;;
    providers.qobuz.binary_sha256) printf '%s\\n' "{qbzd_sha256}" ;;
    *) return 1 ;;
  esac
}}
confirm() {{ return 0; }}
log() {{ :; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
PRESERVE_INSTALL_STATE=0
INSTALL_STATE_FILE={install_state}
restore_qbzd_volume_mode_if_owned
printf 'mode=%s preserve=%s\\n' "$(<"$MODE_FILE")" "$PRESERVE_INSTALL_STATE"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                env={**os.environ, "MODE_FILE": str(mode_file), "QBZD_BINARY": str(fake_qbzd)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=software preserve=0", result.stdout)
            self.assertIn('"volume_mode_changed_by_fxroute": false', install_state.read_text())

    def test_spotifyd_config_and_service_use_fixed_zeroconf_port(self):
        self.assertRegex(self.install, r'SPOTIFYD_ZEROCONF_PORT="[1-9][0-9]{3,4}"')
        config = extract_function(self.install, "write_spotifyd_config")
        service = extract_function(self.install, "configure_spotifyd_service")
        self.assertIn("zeroconf_port = ${SPOTIFYD_ZEROCONF_PORT}", config)
        self.assertNotIn("<<'EOF'", config)
        self.assertIn("--zeroconf-port", service)
        self.assertIn("SPOTIFYD_ZEROCONF_PORT", self.install)

    def test_non_executable_provider_paths_are_detected_and_preserved(self):
        spotifyd_path = extract_function(self.install, "spotifyd_binary_path")
        qbzd_path = extract_function(self.install, "qbzd_binary_path")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            provider_dir = home / ".local" / "bin"
            provider_dir.mkdir(parents=True)
            (provider_dir / "spotifyd").write_text("not executable")
            (provider_dir / "qbzd").write_text("not executable")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"{spotifyd_path}\n{qbzd_path}\nprintf '%s\\n' \"$(spotifyd_binary_path)\" \"$(qbzd_binary_path)\"\n",
                ],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [str(provider_dir / "spotifyd"), str(provider_dir / "qbzd")],
            )

    def test_mdns_guard_is_not_used_for_spotifyd_combinations(self):
        guard_policy = extract_function(self.install, "mdns_guard_needed")
        harness = f"""
{guard_policy}
run_case() {{
  SELECT_SPOTIFYD=$1
  SPOTIFYD_PRESENT_BEFORE=$2
  SELECT_SPOTIFY_DESKTOP=$3
  SPOTIFY_DESKTOP_PRESENT_BEFORE=$4
  SELECT_QOBUZ=$5
  SPOTIFY_DESKTOP_AVAILABLE=$6
  if mdns_guard_needed; then printf 'yes\\n'; else printf 'no\\n'; fi
}}
run_case 1 0 0 0 1 0
run_case 0 1 1 0 1 1
run_case 0 0 1 0 0 1
run_case 0 0 1 0 1 1
run_case 0 0 0 0 1 0
run_case 0 0 1 0 0 0
"""
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "no\nno\nyes\nno\nno\nno\n")

    def test_provider_user_services_suppress_desktop_only_mdns_guard(self):
        detection = extract_function(self.install, "detect_existing_provider_components")
        guard_policy = extract_function(self.install, "mdns_guard_needed")
        unit_exists = extract_function(self.install, "user_unit_exists")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            unit_dir = home / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "spotifyd.service").write_text("ExecStart=/opt/spotifyd/spotifyd\n")
            harness = f"""
set -e
{unit_exists}
{detection}
{guard_policy}
spotify_desktop_install_method() {{ return 1; }}
spotifyd_binary_path() {{ return 1; }}
qbzd_binary_path() {{ return 1; }}
SELECT_SPOTIFY_DESKTOP=0
SELECT_SPOTIFYD=0
SELECT_QOBUZ=0
SPOTIFY_DESKTOP_PRESENT_BEFORE=0
SPOTIFY_DESKTOP_AVAILABLE=1
SPOTIFYD_PRESENT_BEFORE=0
QBZD_PRESENT_BEFORE=0
SPOTIFYD_PROVIDER_STATUS=''
QOBUZ_PROVIDER_STATUS=''
pass() {{ :; }}
detect_existing_provider_components
if mdns_guard_needed; then printf 'yes\\n'; else printf 'no\\n'; fi
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "no")

    def test_mdns_guard_uses_actual_non_default_target_uid(self):
        start = self.install.index("render_mdns_guard_script() {")
        end = self.install.index("\nremove_mdns_guard_if_present()", start)
        renderer = self.install[start:end]
        result = subprocess.run(
            ["bash", "-c", f'{renderer}\nFXROUTE_TARGET_UID=4242\nrender_mdns_guard_script'],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('USER_ID="4242"', result.stdout)
        self.assertIn("meta skuid ${USER_ID}", result.stdout)
        self.assertIn("flush chain inet ${TABLE} output", result.stdout)
        self.assertNotIn('delete table inet "$TABLE" 2>/dev/null || true', result.stdout)
        self.assertNotIn("paul", result.stdout)
        self.assertNotIn("echo 1000", result.stdout)

    def test_mdns_guard_table_match_rejects_foreign_content(self):
        matcher = extract_function(self.uninstall, "mdns_guard_table_matches")
        valid_table = (
            "table inet fxroute_mdnsguard {\n"
            " chain output {\n"
            "  type filter hook output priority filter + 5; policy accept;\n"
            '  meta skuid 4242 ip daddr 224.0.0.251 udp dport 5353 counter packets 0 bytes 0 drop comment "Block desktop user-space mDNS v4 to keep Avahi host advertisement stable"\n'
            '  meta skuid 4242 ip6 daddr ff02::fb udp dport 5353 counter packets 0 bytes 0 drop comment "Block desktop user-space mDNS v6 to keep Avahi host advertisement stable"\n'
            " }\n"
            "}\n"
        )
        foreign_chain = valid_table.replace(
            " }\n}\n",
            " }\n chain foreign {\n  type filter hook input priority filter; policy accept;\n }\n}\n",
        )
        foreign_rule = valid_table.replace(" }\n}\n", "  counter accept\n }\n}\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            nft = bin_dir / "nft"
            nft.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$1 $2 $3 $4" == "list table inet fxroute_mdnsguard" ]]; then\n'
                '  cat "$NFT_TABLE_FILE"\n'
                "  exit 0\n"
                "fi\n"
                "exit 1\n"
            )
            nft.chmod(0o755)
            harness = f"""
{matcher}
read_install_state_field() {{
  [[ "$1" == lan_comfort.mdns_guard_target_uid ]] && printf '4242\\n'
}}
if mdns_guard_table_matches; then printf 'match\\n'; else printf 'mismatch\\n'; fi
"""
            env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
            outcomes = []
            for table in (valid_table, foreign_chain, foreign_rule):
                table_file = root / "table"
                table_file.write_text(table)
                result = subprocess.run(
                    ["bash", "-c", harness],
                    env={**env, "NFT_TABLE_FILE": str(table_file)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outcomes.append(result.stdout.strip())
            self.assertEqual(outcomes, ["match", "mismatch", "mismatch"])

    def test_mdns_guard_artifacts_require_recorded_checksums(self):
        artifact_matcher = extract_function(self.install, "mdns_guard_artifact_matches")
        matcher = extract_function(self.install, "mdns_guard_artifacts_match")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script_path = root / "fxroute-mdns-guard.sh"
            service_path = root / "fxroute-mdns-guard.service"
            timer_path = root / "fxroute-mdns-guard.timer"
            script_path.write_text('TABLE="fxroute_mdnsguard"\n')
            service_path.write_text("ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply\n")
            timer_path.write_text("Unit=fxroute-mdns-guard.service\n")
            code = (
                artifact_matcher
                + "\n"
                + matcher
                .replace('/usr/local/sbin/fxroute-mdns-guard.sh', str(script_path))
                .replace('/etc/systemd/system/fxroute-mdns-guard.service', str(service_path))
                .replace('/etc/systemd/system/fxroute-mdns-guard.timer', str(timer_path))
                + f"\n"
                f"MDNS_GUARD_SCRIPT_SHA256=\"$(sha256sum '{script_path}' | awk '{{print $1}}')\"\n"
                f"MDNS_GUARD_SERVICE_SHA256=\"$(sha256sum '{service_path}' | awk '{{print $1}}')\"\n"
                f"MDNS_GUARD_TIMER_SHA256=\"$(sha256sum '{timer_path}' | awk '{{print $1}}')\"\n"
                "if mdns_guard_artifacts_match; then printf 'match\\n'; else printf 'mismatch\\n'; fi\n"
                f"printf '# changed\\n' >> '{script_path}'\n"
                "if mdns_guard_artifacts_match; then printf 'match\\n'; else printf 'mismatch\\n'; fi\n"
            )
            result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "match\nmismatch\n")
        self.assertNotIn('"$script_path" remove', self.install)
        self.assertNotIn('"$script_path" remove', self.uninstall)
        self.assertIn("mdns_guard_table_matches()", self.install)
        self.assertIn("mdns_guard_table_matches()", self.uninstall)
        self.assertIn("ip daddr 224.0.0.251", self.install)
        self.assertIn("ip6 daddr ff02::fb", self.install)

    def test_target_identity_uses_actual_non_default_user_uid(self):
        body = extract_function(self.install, "determine_fxroute_target_identity")
        code = f"""
{body}
id() {{
  case "$*" in
    '-u fxroute-test') printf '4242\\n' ;;
    '-un') printf 'fxroute-test\\n' ;;
    *) printf '0\\n' ;;
  esac
}}
SUDO_USER=''
FXROUTE_TARGET_USER=''
FXROUTE_TARGET_UID=''
determine_fxroute_target_identity
printf '%s %s\\n' "$FXROUTE_TARGET_USER" "$FXROUTE_TARGET_UID"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fxroute-test 4242")

    def test_firewall_contract_tracks_each_backend_rule_separately(self):
        for rule in (
            "http_80_tcp",
            "https_443_tcp",
            "mdns_5353_udp",
            "fxroute_http_8000_tcp",
            "spotifyd_zeroconf_4444_tcp",
        ):
            self.assertIn(rule, self.install)
            self.assertIn(rule, self.uninstall)
        for function_name in (
            "ensure_firewalld_rule",
            "ensure_ufw_rule",
            "remove_owned_firewalld_rule",
            "remove_owned_ufw_rule",
        ):
            self.assertIn(f"{function_name}()", self.install + self.uninstall)
        self.assertIn("--add-rich-rule", self.install)
        self.assertIn("--remove-rich-rule", self.uninstall)
        self.assertIn("--force delete allow", self.uninstall)
        self.assertIn('priority="100"', extract_function(self.install, "firewalld_rule_rich_rule"))
        self.assertNotIn('priority="-100"', extract_function(self.install, "firewalld_rule_rich_rule"))

    def test_firewalld_existing_port_is_not_owned(self):
        ports = extract_function(self.install, "firewall_rule_port")
        state = extract_function(self.install, "firewall_rule_state_var")
        marker = extract_function(self.install, "mark_firewall_rule_owned")
        query_status = extract_function(self.install, "firewalld_query_status")
        query = extract_function(self.install, "firewalld_query_port")
        rich_rule = extract_function(self.install, "firewalld_rule_rich_rule")
        legacy_rich_rule = extract_function(self.install, "firewalld_legacy_rich_rule")
        rich_query = extract_function(self.install, "firewalld_query_rich_rule")
        rule_query = extract_function(self.install, "firewalld_query_rule")
        ensure = extract_function(self.install, "ensure_firewalld_rule")
        code = f"""
{ports}
{state}
{marker}
{query_status}
{query}
{rich_rule}
{legacy_rich_rule}
{rich_query}
{rule_query}
{ensure}
SUDO_CMD=()
FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=0
log() {{ :; }}
pass() {{ :; }}
warn() {{ :; }}
firewall_cmd_path() {{ printf '%s\\n' firewall_cmd; }}
firewalld_is_active() {{ return 0; }}
firewall_cmd() {{
  case "$*" in
    *--query-rich-rule=*) return 1 ;;
    *--query-port=8000/tcp) return 0 ;;
    *) printf '%s\\n' "$*"; return 0 ;;
  esac
}}
ensure_firewalld_rule fxroute_http_8000_tcp test
printf 'owned=%s\\n' "$FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "owned=0")

    def test_firewalld_existing_builtin_service_is_not_owned(self):
        ports = extract_function(self.install, "firewall_rule_port")
        state = extract_function(self.install, "firewall_rule_state_var")
        marker = extract_function(self.install, "mark_firewall_rule_owned")
        query_status = extract_function(self.install, "firewalld_query_status")
        query = extract_function(self.install, "firewalld_query_port")
        rich_rule = extract_function(self.install, "firewalld_rule_rich_rule")
        legacy_rich_rule = extract_function(self.install, "firewalld_legacy_rich_rule")
        rich_query = extract_function(self.install, "firewalld_query_rich_rule")
        rule_service = extract_function(self.install, "firewalld_rule_service")
        rule_query = extract_function(self.install, "firewalld_query_rule")
        ensure = extract_function(self.install, "ensure_firewalld_rule")
        code = f"""
{ports}
{state}
{marker}
{query_status}
{query}
{rich_rule}
{legacy_rich_rule}
{rich_query}
{rule_service}
{rule_query}
{ensure}
SUDO_CMD=()
FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE=0
log() {{ :; }}
pass() {{ :; }}
warn() {{ :; }}
firewall_cmd_path() {{ printf '%s\\n' firewall_cmd; }}
firewalld_is_active() {{ return 0; }}
firewall_cmd() {{
  case "$*" in
    *--query-rich-rule=*) return 1 ;;
    *--query-port=80/tcp) return 1 ;;
    *--query-service=http) return 0 ;;
    *) printf '%s\\n' "$*"; return 0 ;;
  esac
}}
ensure_firewalld_rule http_80_tcp test
printf 'owned=%s\\n' "$FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "owned=0")

    def test_firewalld_new_rich_rule_is_owned(self):
        ports = extract_function(self.install, "firewall_rule_port")
        state = extract_function(self.install, "firewall_rule_state_var")
        marker = extract_function(self.install, "mark_firewall_rule_owned")
        query_status = extract_function(self.install, "firewalld_query_status")
        query = extract_function(self.install, "firewalld_query_port")
        rich_rule = extract_function(self.install, "firewalld_rule_rich_rule")
        legacy_rich_rule = extract_function(self.install, "firewalld_legacy_rich_rule")
        rich_query = extract_function(self.install, "firewalld_query_rich_rule")
        rule_query = extract_function(self.install, "firewalld_query_rule")
        ensure = extract_function(self.install, "ensure_firewalld_rule")
        code = f"""
{ports}
{state}
{marker}
{query_status}
{query}
{rich_rule}
{legacy_rich_rule}
{rich_query}
{rule_query}
{ensure}
SUDO_CMD=()
FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=0
FIREWALLD_RULE_FORMAT=legacy-port
log() {{ :; }}
pass() {{ :; }}
warn() {{ :; }}
firewall_cmd_path() {{ printf '%s\\n' firewall_cmd; }}
firewalld_is_active() {{ return 0; }}
firewall_cmd() {{
  case "$*" in
    *--query-rich-rule=*) return 1 ;;
    *--query-port=8000/tcp) return 1 ;;
    *--add-rich-rule=*) printf '%s\\n' "$*"; return 0 ;;
    *--reload) return 0 ;;
    *) return 1 ;;
  esac
}}
ensure_firewalld_rule fxroute_http_8000_tcp test
printf 'owned=%s format=%s\\n' "$FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE" "$FIREWALLD_RULE_FORMAT"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('priority="100"', result.stdout)
        self.assertIn("owned=1 format=rich-priority", result.stdout)

    def test_legacy_firewalld_port_is_migrated_to_rich_rule(self):
        ports = extract_function(self.install, "firewall_rule_port")
        state = extract_function(self.install, "firewall_rule_state_var")
        query_status = extract_function(self.install, "firewalld_query_status")
        confirm_migration = extract_function(self.install, "confirm_legacy_firewalld_port_migration")
        migration = extract_function(self.install, "migrate_legacy_firewalld_port")
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "port"
            log_file = Path(td) / "firewall.log"
            state_file.write_text("present")
            code = f"""
{ports}
{state}
{query_status}
{confirm_migration}
{migration}
SUDO_CMD=()
FIREWALLD_RULE_FORMAT=legacy-port
FIREWALLD_LEGACY_PORT_MIGRATION=1
ASSUME_YES=1
FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=1
firewall_rule_owned() {{ [[ "$1:$2" == firewalld:fxroute_http_8000_tcp ]]; }}
firewall_cmd_path() {{ printf '%s\\n' firewall_cmd; }}
firewalld_is_active() {{ return 0; }}
firewall_cmd() {{
  printf '%s\\n' "$*" >> "{log_file}"
  case "$*" in
    --query-port=8000/tcp|--permanent\\ --query-port=8000/tcp) [[ -s "{state_file}" ]] ;;
    --remove-port=8000/tcp) return 0 ;;
    --permanent\\ --remove-port=8000/tcp) : > "{state_file}" ;;
    --reload) return 0 ;;
    *) return 1 ;;
  esac
}}
if migrate_legacy_firewalld_port fxroute_http_8000_tcp; then printf 'migrated=1\\n'; else printf 'migrated=0 status=%s\\n' "$?"; fi
printf 'remaining=%s\\n' "$(<"{state_file}")"
"""
            result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("migrated=1", result.stdout)
            self.assertIn("remaining=", result.stdout)
            self.assertIn("--remove-port=8000/tcp", log_file.read_text())
            self.assertIn("--permanent --remove-port=8000/tcp", log_file.read_text())

    def test_uninstaller_removes_owned_legacy_firewalld_port(self):
        ports = extract_function(self.uninstall, "firewall_rule_port")
        rich_rule = extract_function(self.uninstall, "firewalld_rule_rich_rule")
        legacy_rich_rule = extract_function(self.uninstall, "firewalld_legacy_rich_rule")
        query_status = extract_function(self.uninstall, "firewalld_query_status")
        remover = extract_function(self.uninstall, "remove_owned_firewalld_rule")
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "port"
            log_file = Path(td) / "firewall.log"
            state_file.write_text("present")
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            firewall = bin_dir / "firewall-cmd"
            firewall.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FIREWALLD_LOG\"\n"
                "case \"$*\" in\n"
                "  --state) exit 0 ;;\n"
                "  --query-port=8000/tcp|--permanent\\ --query-port=8000/tcp) [[ -s \"$FIREWALLD_STATE\" ]] ;;\n"
                "  --remove-port=8000/tcp) exit 0 ;;\n"
                "  --permanent\\ --remove-port=8000/tcp) : > \"$FIREWALLD_STATE\" ;;\n"
                "  --reload) exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            firewall.chmod(0o755)
            sudo = bin_dir / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            sudo.chmod(0o755)
            code = f"""
{ports}
{rich_rule}
{legacy_rich_rule}
{query_status}
{remover}
read_install_state_field() {{
  case "$1" in
    lan_comfort.firewalld_rule_format) printf 'legacy-port\\n' ;;
    lan_comfort.firewall_ownership_schema) printf '3\\n' ;;
    lan_comfort.firewalld_owned_rules.fxroute_http_8000_tcp) printf 'true\\n' ;;
    *) return 1 ;;
  esac
}}
firewalld_rule_is_owned() {{ return 0; }}
confirm() {{ return 0; }}
firewall_cleanup_sudo() {{ return 0; }}
firewall_cmd_path() {{ command -v firewall-cmd; }}
firewall_offline_cmd_path() {{ return 1; }}
log() {{ :; }}
warn() {{ :; }}
PRESERVE_INSTALL_STATE=0
remove_owned_firewalld_rule fxroute_http_8000_tcp test
printf 'preserve=%s remaining=%s\\n' "$PRESERVE_INSTALL_STATE" "$(<"{state_file}")"
"""
            result = subprocess.run(
                ["bash", "-c", code],
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FIREWALLD_STATE": str(state_file),
                    "FIREWALLD_LOG": str(log_file),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preserve=0 remaining=", result.stdout)
            self.assertIn("--permanent --remove-port=8000/tcp", log_file.read_text())

    def test_legacy_firewall_ownership_survives_reinstall_state_migration(self):
        rule_port = extract_function(self.uninstall, "firewall_rule_port")
        legacy_field = extract_function(self.uninstall, "legacy_firewall_rule_field")
        legacy_baseline = extract_function(self.uninstall, "legacy_firewalld_baseline_field")
        legacy_owned = extract_function(self.uninstall, "firewalld_legacy_rule_is_owned")
        legacy_state = extract_function(self.uninstall, "firewall_legacy_state_present")
        owned = extract_function(self.uninstall, "firewalld_rule_is_owned")
        code = f"""
{rule_port}
{legacy_field}
{legacy_baseline}
{legacy_owned}
{legacy_state}
{owned}
read_install_state_field() {{
  case "$1" in
    lan_comfort.firewall_ownership_schema) printf '2\\n' ;;
    lan_comfort.legacy_firewall_ownership_present) printf '%s\\n' "$LEGACY" ;;
    lan_comfort.firewalld_owned_rules.http_80_tcp) printf 'false\\n' ;;
    lan_comfort.firewalld_was_active_before) printf 'true\\n' ;;
    lan_comfort.http_opened_by_fxroute) printf 'true\\n' ;;
    lan_comfort.http_was_allowed_before) printf 'false\\n' ;;
    *) return 1 ;;
  esac
}}
if firewalld_rule_is_owned http_80_tcp; then printf 'owned\\n'; else printf 'not-owned\\n'; fi
"""
        legacy = subprocess.run(
            ["bash", "-c", code],
            env={**os.environ, "LEGACY": "true"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(legacy.stdout.strip(), "not-owned")
        current = subprocess.run(
            ["bash", "-c", code],
            env={**os.environ, "LEGACY": "false"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(current.stdout.strip(), "not-owned")

    def test_ufw_new_port_is_owned(self):
        ports = extract_function(self.install, "firewall_rule_port")
        state = extract_function(self.install, "firewall_rule_state_var")
        marker = extract_function(self.install, "mark_firewall_rule_owned")
        ensure = extract_function(self.install, "ensure_ufw_rule")
        code = f"""
{ports}
{state}
{marker}
{ensure}
SUDO_CMD=()
UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=0
SPOTIFYD_ZEROCONF_PORT=4444
log() {{ :; }}
pass() {{ :; }}
warn() {{ :; }}
        ufw_is_active() {{ return 0; }}
        ufw() {{
  if [[ $1 == status ]]; then printf 'Status: active\\n'; return 0; fi
  printf '%s\\n' "$*"
}}
ensure_ufw_rule spotifyd_zeroconf_4444_tcp test
printf 'owned=%s\\n' "$UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("allow 4444/tcp", result.stdout)
        self.assertIn("owned=1", result.stdout)

    def test_uninstaller_removes_only_owned_ufw_rule(self):
        ports = extract_function(self.uninstall, "firewall_rule_port")
        rule_line = extract_function(self.uninstall, "ufw_rule_line")
        ownership_state = extract_function(self.uninstall, "ufw_rule_ownership_state")
        remover = extract_function(self.uninstall, "remove_owned_ufw_rule")
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            ufw_log = Path(td) / "ufw.log"
            ufw = bin_dir / "ufw"
            ufw.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == status ]]; then printf '%b' \"$UFW_STATUS\"; exit 0; fi\n"
                "if [[ $1 == show && $2 == added ]]; then printf \"ufw allow 4444/tcp comment 'spotifyd Zeroconf TCP authentication'\\n\"; exit 0; fi\n"
                "printf '%s\\n' \"$*\" >> \"$UFW_LOG\"\n"
            )
            ufw.chmod(0o755)
            sudo = bin_dir / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            sudo.chmod(0o755)

            def run_case(owned: str):
                code = f"""
{ports}
{rule_line}
{ownership_state}
{remover}
SPOTIFYD_ZEROCONF_PORT=4444
read_install_state_field() {{
  if [[ $1 == lan_comfort.ufw_owned_rules.spotifyd_zeroconf_4444_tcp ]]; then printf '%s\\n' "$OWNED"; else return 1; fi
}}
ufw_rule_is_owned() {{ [[ "$OWNED" == true ]]; }}
confirm() {{ return 0; }}
firewall_cleanup_sudo() {{ return 0; }}
log() {{ :; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
PRESERVE_INSTALL_STATE=0
remove_owned_ufw_rule spotifyd_zeroconf_4444_tcp test
printf 'preserve=%s\\n' "$PRESERVE_INSTALL_STATE"
"""
                environment = os.environ.copy()
                environment.update(
                    {
                        "PATH": f"{bin_dir}:{environment['PATH']}",
                        "OWNED": owned,
                        "UFW_STATUS": "4444/tcp ALLOW Anywhere\n",
                        "UFW_LOG": str(ufw_log),
                    }
                )
                return subprocess.run(
                    ["bash", "-c", code],
                    env=environment,
                    capture_output=True,
                    text=True,
                )

            existing = run_case("false")
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertFalse(ufw_log.exists())

            owned = run_case("true")
            self.assertEqual(owned.returncode, 0, owned.stderr)
            self.assertIn("--force delete allow 4444/tcp", ufw_log.read_text())
            self.assertIn("preserve=0", owned.stdout)

    def test_uninstaller_preserves_state_when_inactive_ufw_keeps_persistent_rule(self):
        ports = extract_function(self.uninstall, "firewall_rule_port")
        rule_line = extract_function(self.uninstall, "ufw_rule_line")
        ownership_state = extract_function(self.uninstall, "ufw_rule_ownership_state")
        remover = extract_function(self.uninstall, "remove_owned_ufw_rule")
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            ufw = bin_dir / "ufw"
            ufw.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == show && $2 == added ]]; then printf 'ufw allow 4444/tcp\\n'; exit 0; fi\n"
                "exit 1\n"
            )
            ufw.chmod(0o755)
            sudo = bin_dir / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            sudo.chmod(0o755)
            code = f"""
{ports}
{rule_line}
{ownership_state}
{remover}
SPOTIFYD_ZEROCONF_PORT=4444
ufw_rule_is_owned() {{ return 0; }}
confirm() {{ return 0; }}
firewall_cleanup_sudo() {{ return 0; }}
log() {{ :; }}
warn() {{ :; }}
PRESERVE_INSTALL_STATE=0
remove_owned_ufw_rule spotifyd_zeroconf_4444_tcp test
printf 'preserve=%s\\n' "$PRESERVE_INSTALL_STATE"
"""
            result = subprocess.run(
                ["bash", "-c", code],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preserve=1", result.stdout)

    def test_uninstaller_preserves_state_when_ufw_cannot_be_verified(self):
        ports = extract_function(self.uninstall, "firewall_rule_port")
        legacy_field = extract_function(self.uninstall, "legacy_ufw_rule_field")
        rule_line = extract_function(self.uninstall, "ufw_rule_line")
        ownership_state = extract_function(self.uninstall, "ufw_rule_ownership_state")
        owned = extract_function(self.uninstall, "ufw_rule_is_owned")
        remover = extract_function(self.uninstall, "remove_owned_ufw_rule")
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            ufw = bin_dir / "ufw"
            ufw.write_text("#!/usr/bin/env bash\nexit 1\n")
            ufw.chmod(0o755)
            code = f"""
{ports}
{legacy_field}
{rule_line}
{ownership_state}
{owned}
{remover}
SPOTIFYD_ZEROCONF_PORT=4444
read_install_state_field() {{
  case "$1" in
    lan_comfort.ufw_owned_rules.http_80_tcp) return 1 ;;
    lan_comfort.http_opened_by_fxroute) printf 'true\\n' ;;
    *) return 1 ;;
  esac
}}
firewall_legacy_state_present() {{ return 0; }}
confirm() {{ return 0; }}
firewall_cleanup_sudo() {{ return 0; }}
log() {{ :; }}
warn() {{ :; }}
PRESERVE_INSTALL_STATE=0
remove_owned_ufw_rule http_80_tcp test
printf 'preserve=%s\\n' "$PRESERVE_INSTALL_STATE"
"""
            result = subprocess.run(
                ["bash", "-c", code],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preserve=1", result.stdout)

    def test_uninstaller_removes_only_owned_firewalld_port(self):
        ports = extract_function(self.uninstall, "firewall_rule_port")
        rich_rule = extract_function(self.uninstall, "firewalld_rule_rich_rule")
        legacy_rich_rule = extract_function(self.uninstall, "firewalld_legacy_rich_rule")
        query_status = extract_function(self.uninstall, "firewalld_query_status")
        remover = extract_function(self.uninstall, "remove_owned_firewalld_rule")
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            firewall_log = Path(td) / "firewall.log"
            firewall_state = Path(td) / "firewall-state"
            firewall_state.write_text("present")
            firewall = bin_dir / "firewall-cmd"
            firewall.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  --state) exit 0 ;;\n"
                "  *--query-rich-rule=*)\n"
                "    [[ -s \"$FIREWALLD_STATE\" ]] && exit 0 || exit 1\n"
                "    ;;\n"
                "  *--query-port=8000/tcp) exit 1 ;;\n"
                "  *--remove-rich-rule=*)\n"
                "    [[ $* == *--permanent* ]] && : > \"$FIREWALLD_STATE\"\n"
                "    ;;\n"
                "esac\n"
                "printf '%s\\n' \"$*\" >> \"$FIREWALLD_LOG\"\n"
            )
            firewall.chmod(0o755)
            sudo = bin_dir / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            sudo.chmod(0o755)

            def run_case(owned: str):
                code = f"""
{ports}
{rich_rule}
{legacy_rich_rule}
{query_status}
{remover}
read_install_state_field() {{
  case "$1" in
    lan_comfort.firewalld_owned_rules.fxroute_http_8000_tcp) printf '%s\\n' "$OWNED" ;;
    lan_comfort.firewalld_rule_format) printf 'rich-priority\\n' ;;
    *) return 1 ;;
  esac
}}
firewalld_rule_is_owned() {{ [[ "$OWNED" == true ]]; }}
confirm() {{ return 0; }}
firewall_cleanup_sudo() {{ return 0; }}
firewall_cmd_path() {{ command -v firewall-cmd; }}
firewall_offline_cmd_path() {{ return 1; }}
log() {{ :; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
PRESERVE_INSTALL_STATE=0
remove_owned_firewalld_rule fxroute_http_8000_tcp test
printf 'preserve=%s\\n' "$PRESERVE_INSTALL_STATE"
"""
                environment = os.environ.copy()
                environment.update(
                    {
                        "PATH": f"{bin_dir}:{environment['PATH']}",
                        "OWNED": owned,
                        "FIREWALLD_STATE": str(firewall_state),
                        "FIREWALLD_LOG": str(firewall_log),
                    }
                )
                return subprocess.run(
                    ["bash", "-c", code],
                    env=environment,
                    capture_output=True,
                    text=True,
                )

            existing = run_case("false")
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertEqual(firewall_state.read_text(), "present")

            owned = run_case("true")
            self.assertEqual(owned.returncode, 0, owned.stderr)
            self.assertIn("--permanent --remove-rich-rule=rule priority=", firewall_log.read_text())
            self.assertEqual(firewall_state.read_text(), "")
            self.assertIn("preserve=0", owned.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
