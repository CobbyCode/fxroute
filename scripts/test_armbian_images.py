#!/usr/bin/env python3
"""Regression tests for the official Armbian image build path."""

import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARMBIAN_DIR = ROOT / "armbian"
BUILD_SH = ARMBIAN_DIR / "build-image.sh"
CUSTOMIZE_SH = ARMBIAN_DIR / "customize-image.sh"
FIRST_BOOT_SH = ARMBIAN_DIR / "first-boot-install.sh"
SERVICE = ARMBIAN_DIR / "fxroute-armbian-first-boot.service"
WEB_CONFIG_SH = ARMBIAN_DIR / "armbian-web-config.py"
WEB_CONFIG_SERVICE = ARMBIAN_DIR / "armbian-web-config.service"
QEMU_SH = ARMBIAN_DIR / "test-image.sh"


class ArmbianImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = BUILD_SH.read_text()
        cls.customize = CUSTOMIZE_SH.read_text()
        cls.first_boot = FIRST_BOOT_SH.read_text()
        cls.service = SERVICE.read_text()
        cls.web_config = WEB_CONFIG_SH.read_text() if WEB_CONFIG_SH.exists() else ""
        cls.web_config_service = (
            WEB_CONFIG_SERVICE.read_text() if WEB_CONFIG_SERVICE.exists() else ""
        )
        cls.qemu = QEMU_SH.read_text()

    def test_build_wrapper_uses_pinned_official_armbian_source(self):
        self.assertIn("https://github.com/armbian/build.git", self.build)
        self.assertRegex(
            self.build,
            r'ARMBIAN_BUILD_REF="[0-9a-f]{40}"',
        )
        self.assertIn("fetch --depth=1", self.build)
        self.assertIn("git checkout --detach", self.build)
        self.assertIn('git -C "$repository" checkout --detach "$ARMBIAN_BUILD_REF"', self.build)
        self.assertIn('"$armbian_dir/compile.sh"', self.build)

    def test_build_wrapper_stages_reproducible_source_without_builder_credentials(self):
        self.assertIn("SOURCE_DATE_EPOCH", self.build)
        self.assertIn("ls-files -z", self.build)
        self.assertIn("--sort=name", self.build)
        self.assertIn("userpatches/overlay", self.build)
        self.assertIn("customize-image.sh", self.build)
        self.assertIn("source.tar", self.build)
        self.assertNotIn("password-hash", self.build)
        self.assertNotIn("ssh-public-key-file", self.build)
        self.assertNotIn("FXROUTE_PASSWORD_HASH", self.build)
        self.assertNotIn("FXROUTE_SSH_PUBLIC_KEY", self.build)
        self.assertNotIn("provision.env", self.build)

    def test_build_wrapper_stages_a_private_per_image_wifi_setup_password(self):
        self.assertIn("--wifi-setup-password", self.build)
        self.assertIn("generate_wifi_setup_password", self.build)
        self.assertIn("ARMBIAN_WEB_CONFIG_AP_PASSWORD_VERIFIER_B64", self.build)
        self.assertIn("ARMBIAN_WEB_CONFIG_AP_PSK", self.build)
        self.assertIn("ARMBIAN_WEB_CONFIG_AP_SSID", self.build)
        self.assertIn("armbian-web-config.env", self.build)
        self.assertIn("armbian-web-config.env", self.customize)
        self.assertIn("EnvironmentFile=/etc/default/armbian-web-config", self.web_config_service)
        self.assertNotIn('AP_PASSWORD = "armbian1234"', self.web_config)

    def test_build_wrapper_exports_epoch_before_generating_hooks(self):
        self.assertRegex(
            self.build,
            r'SOURCE_DATE_EPOCH="\$\{SOURCE_DATE_EPOCH:-0\}"\nexport SOURCE_DATE_EPOCH',
        )

    def test_first_boot_reads_only_the_user_created_by_end_user_onboarding(self):
        self.assertIn("/var/lib/armbian-web-config/account", self.first_boot)
        self.assertIn('CONFIGURED_MARKER="/var/lib/armbian-web-config/configured"', self.first_boot)
        self.assertIn("read_account_user", self.first_boot)
        self.assertNotIn("PROVISION_FILE", self.first_boot)
        self.assertNotIn("FXROUTE_PASSWORD_HASH", self.first_boot)
        self.assertNotIn("FXROUTE_SSH_PUBLIC_KEY", self.first_boot)
        self.assertNotIn("read_provision_value", self.first_boot)
        self.assertIn("/var/lib/armbian-web-config/account", self.web_config)

    def test_build_wrapper_has_raspberry_aliases_and_generic_board_support(self):
        self.assertIn('rpi4|rpi4b|rpi5|rpi5b)', self.build)
        self.assertIn('build_board="rpi4b"', self.build)
        self.assertIn('requested_board="$board"', self.build)
        self.assertIn("config/boards/$build_board.conf", self.build)
        self.assertIn("--board <board>", self.build)

    def test_build_wrapper_discovers_all_armbian_board_file_types(self):
        self.assertIn(
            'local -a board_types=("conf" "wip" "csc" "eos" "tvb")',
            self.build,
        )
        self.assertIn("(export[[:space:]]+)?BOARDFAMILY", self.build)
        self.assertIn("sub(/[[:space:]]+#.*/, \"\", value)", self.build)

    def test_build_wrapper_rejects_partial_armbian_sources(self):
        self.assertIn("--is-shallow-repository", self.build)
        self.assertIn("extensions.partialClone", self.build)

    def test_family_override_keeps_networking_headless_after_board_config(self):
        self.assertIn('config/sources/families/$family.conf', self.build)
        self.assertIn('declare -g NETWORKING_STACK="systemd-networkd"', self.build)
        self.assertIn("NETWORKING_STACK=systemd-networkd", self.build)

    def test_headless_build_configuration_disables_desktop(self):
        self.assertIn("BUILD_MINIMAL=yes", self.build)
        self.assertIn("BUILD_DESKTOP=no", self.build)
        self.assertIn("NETWORKING_STACK=systemd-networkd", self.build)
        self.assertIn("CONSOLE_AUTOLOGIN=no", self.build)
        self.assertIn("DESKTOP_AUTOLOGIN=no", self.build)
        self.assertNotIn("BUILD_DESKTOP=yes", self.build)
        self.assertNotIn("plasma", self.build.lower())
        self.assertNotIn("chrome", self.build.lower())

    def test_build_wrapper_reserves_room_for_first_boot_install(self):
        self.assertIn("FIXED_IMAGE_SIZE=8192", self.build)

    def test_image_packages_use_armbian_package_registration(self):
        self.assertIn("add_packages_to_image", self.build)
        self.assertNotIn("EXTRA_PACKAGES_IMAGE=(", self.build)

    def test_raspberry_boot_staging_avoids_fat_security_xattrs(self):
        self.assertIn("config_post_debootstrap_tweaks", self.build)
        self.assertNotIn("setfattr", self.build)

    def test_raspberry_boot_staging_filters_security_xattrs_for_rsync(self):
        self.assertIn("fxroute-rsync", self.build)
        self.assertIn("--filter='-x security.*'", self.build)
        self.assertIn("--filter='-x system.*'", self.build)
        self.assertIn("boot_copy=0", self.build)
        self.assertIn("case \"$arg\" in", self.build)

    def test_raspberry_boot_staging_uses_a_valid_fat_wrapper_link(self):
        self.assertIn('ln -sfn -- mkfs.fat "$wrapper_dir/mkfs.vfat"', self.build)
        self.assertNotIn('ln -srf mkfs.fat "$wrapper_dir/mkfs.vfat"', self.build)

    def test_xattr_host_dependency_is_visible_to_docker_launcher(self):
        self.assertIn("userpatches/config-fxroute.conf", self.build)
        self.assertIn("type -P rsync", self.build)

    def test_image_filesystem_metadata_uses_reproducible_inputs(self):
        self.assertIn("E2FSPROGS_FAKE_TIME", self.build)
        self.assertIn("--invariant", self.build)
        self.assertIn("hash_seed=", self.build)
        self.assertIn("fake-hwclock.data", self.build)
        self.assertIn("date -u '+%Y-%m-%d %H:%M:%S'", self.build)
        self.assertIn("BOOTSCRIPT_TEMPLATE__CREATE_DATE", self.build)

    def test_build_wrapper_preserves_qemu_companion_artifacts(self):
        self.assertIn("*.img.qcow2", self.build)
        self.assertIn("*.u-boot.bin", self.build)
        self.assertIn("artifact_stem", self.build)

    def test_build_uuid_is_unique_per_temporary_workspace(self):
        self.assertIn("WORK_TOKEN=", self.build)
        self.assertIn('ARMBIAN_BUILD_UUID="fxroute-${requested_board}-${RELEASE}-${BRANCH}-${WORK_TOKEN}"', self.build)
        self.assertNotIn(
            'ARMBIAN_BUILD_UUID="fxroute-${requested_board}-${RELEASE}-${BRANCH}"',
            self.build,
        )

    def test_customize_script_installs_only_the_first_boot_handoff(self):
        self.assertIn('BUILD_DESKTOP="${4:-}"', self.customize)
        self.assertIn('ARCH="${5:-}"', self.customize)
        self.assertIn('OVERLAY_DIR="/tmp/overlay"', self.customize)
        self.assertIn('"$OVERLAY_DIR/source.tar"', self.customize)
        self.assertNotIn('"$OVERLAY_DIR/provision.env"', self.customize)
        self.assertIn("/opt/fxroute-armbian", self.customize)
        self.assertIn("fxroute-armbian-first-boot.service", self.customize)
        self.assertIn("multi-user.target.wants", self.customize)
        self.assertIn("passwd -l root", self.customize)
        self.assertNotIn("rm -f -- /root/.not_logged_in_yet", self.customize)
        self.assertIn("/etc/profile.d/armbian-check-first-login.sh", self.customize)
        self.assertIn("install -d -m 755 /usr/local/libexec", self.customize)
        self.assertNotIn("systemctl --user", self.customize)
        self.assertIn("armbian-firstrun.service.d", self.customize)
        self.assertIn("Type=oneshot", self.customize)
        self.assertIn("rm -f /etc/ssh/ssh_host_*", self.customize)
        self.assertIn("systemctl is-active --quiet armbian-web-config.service", self.customize)

    def test_headless_onboarding_retains_armbian_first_login_marker(self):
        """The web service needs Armbian's marker, including on Ethernet boots."""
        self.assertNotIn("/root/.not_logged_in_yet /etc/profile.d", self.customize)
        self.assertIn("ConditionPathExists=/root/.not_logged_in_yet", self.web_config_service)
        self.assertIn(
            "ConditionPathExists=!/var/lib/armbian-web-config/configured",
            self.web_config_service,
        )

    def test_headless_onboarding_uses_the_pinned_armbian_ssid_contract(self):
        self.assertIn("AP_SSID_SUFFIX = \"-armbiansetup\"", self.web_config)
        self.assertIn("10.42.0.1", self.web_config)
        self.assertNotIn("armbiansetup-{hostname}", self.web_config)

    def test_headless_onboarding_prefers_ethernet_before_starting_the_ap(self):
        self.assertIn("has_ethernet_carrier", self.web_config)
        self.assertIn("wait_for_ethernet", self.web_config)
        self.assertIn("start_access_point", self.web_config)
        self.assertLess(
            self.web_config.index("has_ethernet_carrier"),
            self.web_config.index("start_access_point"),
        )
        self.assertIn("route-metric: 100", self.customize)
        self.assertIn("route-metric: 600", self.web_config)

    def test_wifi_only_onboarding_writes_native_networkd_netplan(self):
        self.assertIn("30-wifis-dhcp.yaml", self.web_config)
        self.assertIn("renderer: networkd", self.web_config)
        self.assertIn('["netplan", "apply"]', self.web_config)
        self.assertNotIn('["netplan", "apply", "--timeout", "0"]', self.web_config)
        self.assertIn("wpasupplicant", self.build)
        self.assertIn("hostapd", self.build)
        self.assertIn("dnsmasq-base", self.build)
        self.assertIn("openssl", self.build)
        self.assertIn("wpa_psk", self.web_config)
        self.assertNotIn("wpa_passphrase", self.web_config)

    def test_web_config_service_is_ordered_before_fxroute_provisioning(self):
        self.assertIn("Before=network-online.target", self.web_config_service)
        self.assertIn("After=network.target armbian-firstrun.service", self.web_config_service)
        self.assertIn("Type=oneshot", self.web_config_service)
        self.assertIn("RemainAfterExit=yes", self.web_config_service)
        self.assertIn("armbian-web-config.service", self.service)
        self.assertIn("Wants=network-online.target armbian-firstrun.service armbian-web-config.service", self.service)
        self.assertNotIn("Requires=armbian-web-config.service", self.service)
        self.assertIn("After=network-online.target armbian-firstrun.service ssh.service armbian-web-config.service", self.service)
        self.assertIn("Wants=network-online.target armbian-firstrun.service", self.service)
        self.assertNotIn("ConditionPathExists=/opt/fxroute-armbian/source.tar", self.service)
        self.assertIn("/root/.not_logged_in_yet", self.first_boot)
        self.assertIn("systemctl disable armbian-web-config.service", self.first_boot)
        self.assertNotIn("disable --now armbian-web-config.service", self.first_boot)

    def test_first_boot_provisions_user_and_calls_existing_installer(self):
        self.assertIn("getent passwd", self.first_boot)
        self.assertNotIn("provision_user", self.first_boot)
        self.assertIn('"$SOURCE_DIR/install.sh"', self.first_boot)
        self.assertIn("--source", self.first_boot)
        self.assertIn("--target", self.first_boot)
        self.assertIn("--user", self.first_boot)
        self.assertIn("--providers none", self.first_boot)
        self.assertIn("--yes", self.first_boot)
        self.assertIn("install-complete", self.first_boot)
        self.assertIn("install-failed", self.first_boot)
        self.assertIn("install-in-progress", self.first_boot)
        self.assertIn("wait_for_valid_clock", self.first_boot)
        self.assertIn("System clock did not synchronize", self.first_boot)
        self.assertIn("network-online.target", self.service)
        self.assertIn("systemctl disable", self.first_boot)
        self.assertIn("rm -rf -- /opt/fxroute-armbian", self.first_boot)
        self.assertIn("install-cleanup-pending", self.first_boot)
        self.assertIn("armbian-check-first-login.sh", self.first_boot)
        self.assertIn("systemctl reload-or-restart", self.first_boot)
        self.assertIn("write_durable_marker", self.first_boot)
        self.assertIn('temporary="${path}.tmp"', self.first_boot)
        self.assertNotIn("spotify-desktop", self.first_boot)
        self.assertNotIn("--spotifyd", self.first_boot)

    def test_web_onboarding_creates_the_account_and_ssh_access(self):
        self.assertIn("validate_account", self.web_config)
        self.assertIn("useradd", self.web_config)
        self.assertIn("chpasswd", self.web_config)
        self.assertIn("authorized_keys", self.web_config)
        self.assertIn("sshd", self.web_config)
        self.assertIn("ssh-keygen", self.web_config)
        self.assertIn("validate_setup_password", self.web_config)
        self.assertIn("pbkdf2-sha256", self.web_config)
        self.assertIn('name="setup_password"', self.web_config)
        self.assertIn('"36500"', self.web_config)
        self.assertIn("begin_setup_response", self.web_config)
        self.assertIn("send_response(202)", self.web_config)

    def test_first_boot_service_is_retryable_and_headless(self):
        self.assertIn("After=network-online.target armbian-firstrun.service", self.service)
        self.assertIn("Wants=network-online.target armbian-firstrun.service", self.service)
        self.assertIn("Type=oneshot", self.service)
        self.assertIn("Restart=on-failure", self.service)
        self.assertIn("TimeoutStartSec=2h", self.service)
        self.assertIn("WantedBy=multi-user.target", self.service)
        self.assertNotIn("graphical.target", self.service)

    def test_first_boot_waits_for_armbian_first_run_process(self):
        self.assertIn(
            "systemctl show --property=SubState --value armbian-firstrun.service",
            self.first_boot,
        )
        self.assertRegex(
            self.first_boot,
            r'while \[\[ "\$\(systemctl show .*armbian-firstrun\.service.*\)" == "running" \]\]; do',
        )
        self.assertLess(
            self.first_boot.index("armbian-firstrun.service"),
            self.first_boot.index("read_account_user\n"),
        )

    def test_qemu_runner_only_claims_pi4_emulation(self):
        self.assertIn("qemu-system-aarch64", self.qemu)
        self.assertIn("raspi4b", self.qemu)
        self.assertIn("virt", self.qemu)
        self.assertIn("rpi5", self.qemu)
        self.assertIn("hardware", self.qemu.lower())
        self.assertIn("/api/status", self.qemu)
        self.assertIn("fxroute_dsp_sink", self.qemu)
        self.assertIn("systemctl --user", self.qemu)
        self.assertIn("-snapshot", self.qemu)

    def test_qemu_virt_runner_completes_end_user_onboarding(self):
        self.assertIn("ssh-keygen", self.qemu)
        self.assertIn("hostfwd=tcp:127.0.0.1:${setup_port}-:443", self.qemu)
        self.assertIn('https://127.0.0.1:$setup_port/', self.qemu)
        self.assertIn('"username=$FXROUTE_USER"', self.qemu)
        self.assertIn('"account_password=$account_password"', self.qemu)
        self.assertIn('"ssh_key=$ssh_public_key"', self.qemu)
        self.assertIn('"setup_password=$SETUP_PASSWORD"', self.qemu)

    def test_qemu_runner_limits_pi4_to_serial_boot_validation(self):
        self.assertIn("Reached target .*basic\\.target", self.qemu)
        self.assertIn("network/API checks require hardware or generic virt", self.qemu)

    def test_qemu_runner_overlay_inherits_raw_image_size(self):
        self.assertIn("qemu-img", self.qemu)
        self.assertIn('DISK_IMAGE="$WORK_DIR/image.qcow2"', self.qemu)
        self.assertIn(
            'qemu-img create -q -f qcow2 -F raw -b "$IMAGE" "$DISK_IMAGE"\n',
            self.qemu,
        )
        self.assertNotIn(
            'qemu-img create -q -f qcow2 -F raw -b "$IMAGE" "$DISK_IMAGE" 2G',
            self.qemu,
        )
        self.assertIn('file=$DISK_IMAGE,format=qcow2', self.qemu)

    def test_qemu_runner_loads_raspberry_boot_assets_explicitly(self):
        self.assertIn("mcopy", self.qemu)
        self.assertIn('BOOT_PARTITION_OFFSET="4M"', self.qemu)
        self.assertIn('KERNEL_IMAGE="$WORK_DIR/vmlinuz"', self.qemu)
        self.assertIn('DTB_IMAGE="$WORK_DIR/bcm2711-rpi-4-b.dtb"', self.qemu)
        self.assertIn('INITRD_IMAGE="$WORK_DIR/initrd.img"', self.qemu)
        self.assertIn('CMDLINE_FILE="$WORK_DIR/cmdline.txt"', self.qemu)
        self.assertIn('"::vmlinuz" "$KERNEL_IMAGE"', self.qemu)
        self.assertIn('"::bcm2711-rpi-4-b.dtb" "$DTB_IMAGE"', self.qemu)
        self.assertIn('"::initrd.img" "$INITRD_IMAGE"', self.qemu)
        self.assertIn('"::cmdline.txt" "$CMDLINE_FILE"', self.qemu)
        self.assertIn(
            'KERNEL_CMDLINE="${KERNEL_CMDLINE// console=tty1/}"',
            self.qemu,
        )
        self.assertIn("-kernel", self.qemu)
        self.assertIn("-dtb", self.qemu)
        self.assertIn("-initrd", self.qemu)
        self.assertIn("-append", self.qemu)

    def test_qemu_runner_boots_the_generic_virt_machine_with_aarch64_uefi(self):
        virt_section = self.qemu.split('else\n', 1)[1]
        self.assertIn('aavmf-aarch64-code.bin', self.qemu)
        self.assertIn('aavmf-aarch64-vars.bin', self.qemu)
        self.assertIn('UEFI_VARS="$WORK_DIR/aavmf-vars.bin"', self.qemu)
        self.assertIn('-drive "if=pflash,format=raw,readonly=on,file=$UEFI_CODE"', virt_section)
        self.assertIn('-drive "if=pflash,format=raw,file=$UEFI_VARS"', virt_section)

    def test_cleanup_trap_is_non_fatal_when_work_dir_removal_fails(self):
        """A successful build stays exit 0 when leftover root-owned files
        from the Docker build prevent removal of the work directory."""
        lines = self.build.splitlines()
        start = next(
            i for i, line in enumerate(lines) if line == "cleanup() {"
        )
        end = next(i for i in range(start, len(lines)) if lines[i] == "}")
        cleanup_fn = "\n".join(lines[start:end + 1])

        def run_cleanup(extra_lines):
            script = "\n".join(
                [
                    "set -Eeuo pipefail",
                    'WORK_DIR="$(mktemp -d)"',
                    'mkdir -p "$WORK_DIR/rootish"',
                    ': > "$WORK_DIR/rootish/file"',
                    *extra_lines,
                    cleanup_fn,
                    "KEEP_WORK=0",
                    "trap cleanup EXIT",
                    'echo "build ok"',
                ]
            )
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
            )

        # The unremovable entry simulates root-owned leftovers: rm fails, the
        # trap warns instead of aborting, and the build status stays 0.
        guard = run_cleanup(['chmod 555 "$WORK_DIR/rootish"'])
        self.assertEqual(guard.returncode, 0, guard.stderr)
        self.assertIn("[armbian][warn]", guard.stderr)

        # A removable work directory is still cleaned up without a warning.
        clean = run_cleanup([])
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertNotIn("[armbian][warn]", clean.stderr)

    def test_scripts_are_shell_parseable(self):
        for path in (BUILD_SH, CUSTOMIZE_SH, FIRST_BOOT_SH, QEMU_SH):
            result = subprocess.run(
                ["bash", "-n", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

        result = subprocess.run(
            ["python3", "-m", "py_compile", str(WEB_CONFIG_SH)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"{WEB_CONFIG_SH}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
