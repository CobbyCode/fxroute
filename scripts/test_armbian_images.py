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
QEMU_SH = ARMBIAN_DIR / "test-image.sh"


class ArmbianImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = BUILD_SH.read_text()
        cls.customize = CUSTOMIZE_SH.read_text()
        cls.first_boot = FIRST_BOOT_SH.read_text()
        cls.service = SERVICE.read_text()
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

    def test_build_wrapper_stages_reproducible_source_and_credentials(self):
        self.assertIn("SOURCE_DATE_EPOCH", self.build)
        self.assertIn("ls-files -z", self.build)
        self.assertIn("--sort=name", self.build)
        self.assertIn("userpatches/overlay", self.build)
        self.assertIn("customize-image.sh", self.build)
        self.assertIn("source.tar", self.build)
        self.assertIn("password-hash", self.build)
        self.assertIn("ssh-public-key-file", self.build)
        self.assertIn("FXROUTE_PASSWORD_HASH", self.build)
        self.assertIn("FXROUTE_SSH_PUBLIC_KEY", self.build)

    def test_build_wrapper_exports_epoch_before_generating_hooks(self):
        self.assertRegex(
            self.build,
            r'SOURCE_DATE_EPOCH="\$\{SOURCE_DATE_EPOCH:-0\}"\nexport SOURCE_DATE_EPOCH',
        )

    def test_provisioning_metadata_is_encoded_and_decoded_without_sourcing(self):
        self.assertIn("encode_provision_value", self.build)
        self.assertIn("base64 --wrap=0", self.build)
        self.assertIn("FXROUTE_USER_B64", self.build)
        self.assertIn("base64 --decode", self.first_boot)
        self.assertNotIn("base64 --decode --strict", self.first_boot)
        self.assertIn("FXROUTE_SSH_PUBLIC_KEY_B64", self.first_boot)
        self.assertNotIn('source "$PROVISION_FILE"', self.first_boot)
        self.assertNotIn('. "$PROVISION_FILE"', self.first_boot)

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
        self.assertIn('"$OVERLAY_DIR/provision.env"', self.customize)
        self.assertIn("/opt/fxroute-armbian", self.customize)
        self.assertIn("fxroute-armbian-first-boot.service", self.customize)
        self.assertIn("multi-user.target.wants", self.customize)
        self.assertIn("passwd -l root", self.customize)
        self.assertIn("armbian-check-first-login", self.customize)
        self.assertIn("install -d -m 755 /usr/local/libexec", self.customize)
        self.assertNotIn("systemctl --user", self.customize)

    def test_first_boot_provisions_user_and_calls_existing_installer(self):
        self.assertIn("getent passwd", self.first_boot)
        self.assertIn("useradd", self.first_boot)
        self.assertIn("authorized_keys", self.first_boot)
        self.assertIn("usermod -aG", self.first_boot)
        self.assertIn('"$SOURCE_DIR/install.sh"', self.first_boot)
        self.assertIn("--source", self.first_boot)
        self.assertIn("--target", self.first_boot)
        self.assertIn("--user", self.first_boot)
        self.assertIn("--providers none", self.first_boot)
        self.assertIn("--yes", self.first_boot)
        self.assertIn("install-complete", self.first_boot)
        self.assertIn("install-failed", self.first_boot)
        self.assertIn("install-in-progress", self.first_boot)
        self.assertIn("network-online.target", self.service)
        self.assertIn("systemctl disable", self.first_boot)
        self.assertIn("rm -rf -- /opt/fxroute-armbian", self.first_boot)
        self.assertIn("systemctl reload-or-restart", self.first_boot)
        self.assertIn('COMPLETE_MARKER}.tmp', self.first_boot)
        self.assertIn('mv -f -- "$complete_marker_tmp" "$COMPLETE_MARKER"', self.first_boot)
        self.assertNotIn("spotify-desktop", self.first_boot)
        self.assertNotIn("--spotifyd", self.first_boot)

    def test_first_boot_validates_provisioned_values_without_sourcing_them(self):
        self.assertIn("read_provision_value", self.first_boot)
        self.assertIn("^\\$6\\$", self.first_boot)
        self.assertIn("ssh-ed25519", self.first_boot)
        self.assertNotIn("source \"$PROVISION_FILE\"", self.first_boot)
        self.assertNotIn(". \"$PROVISION_FILE\"", self.first_boot)

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
            self.first_boot.index("provision_user\n"),
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

    def test_scripts_are_shell_parseable(self):
        for path in (BUILD_SH, CUSTOMIZE_SH, FIRST_BOOT_SH, QEMU_SH):
            result = subprocess.run(
                ["bash", "-n", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
