#!/usr/bin/env python3
"""Contract tests for the openSUSE Leap 16 FXRoute installation ISO."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ISO_DIR = ROOT / "iso"
PROFILE_DIR = ISO_DIR / "profiles"


class InstallIsoContractTests(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text()

    def test_build_script_pins_leap_media_and_preserves_boot_metadata(self):
        build = self.read("iso/build-leap-16-iso.sh")

        self.assertIn(
            "https://download.opensuse.org/distribution/leap/16.0/offline/"
            "Leap-16.0-offline-installer-x86_64.install.iso",
            build,
        )
        self.assertIn(
            "94411793a1878b3558211c8bbe4f3823c3c5212cc2dd9f9e4532a18be7eddd3be14db5a3bb47a2f36dd957e190ea706cd45af16bce218dfc00f71e64a8469971",
            build,
        )
        self.assertIn("ls-files", build)
        self.assertIn("source.tar", build)
        self.assertIn("--pax-option=delete=atime,delete=ctime", build)
        self.assertIn("mkmedia", build)
        self.assertIn("--add-entry", build)
        self.assertIn("--boot", build)
        self.assertIn("--no-digest", build)
        self.assertIn("FXROUTE_ISO_TMPDIR", build)
        self.assertIn("SOURCE_DATE_EPOCH", build)
        self.assertIn("mkmedia.patch", build)
        self.assertIn("FXROUTE_MKMEDIA_TOOL_DIR", build)
        self.assertIn('FXROUTE_MKMEDIA_TOOL_DIR="$MKMEDIA_TOOL_DIR"', build)
        self.assertIn("mkisofs-reproducible.sh", build)
        self.assertIn("isohybrid-reproducible.sh", build)
        self.assertIn("FXRoute Headless", build)
        self.assertIn("FXRoute Desktop", build)
        self.assertNotIn("label-stage", build)
        self.assertIn("isoinfo -i", build)
        self.assertIn("final-grub.cfg", build)

    def test_mkmedia_patch_keeps_requested_titles_verbatim(self):
        patch = self.read("iso/mkmedia.patch")

        self.assertIn(
            "-          $ent =~ s/$inst_regexp/$1$2 - $opt_new_boot_entry$1/;",
            patch,
        )
        self.assertIn(
            "+          $ent =~ s/$inst_regexp/$1$opt_new_boot_entry$1/;",
            patch,
        )

    def test_builder_uses_mkmedia_hybrid_helper_and_checks_mbr(self):
        build = self.read("iso/build-leap-16-iso.sh")

        self.assertIn('MKMEDIA_ROOT="$(cd -- "$(dirname -- "$MKMEDIA")/.." && pwd)"', build)
        self.assertIn('MKMEDIA_ROOT/libexec/mkmedia/isohybrid', build)
        self.assertIn('[[ "$(od -An -tx1 -j 510 -N 2 "$FINAL_ISO"', build)

    def test_reproducible_tool_shims_pin_dates_and_hybrid_id(self):
        build = self.read("iso/build-leap-16-iso.sh")
        mkisofs = self.read("iso/mkisofs-reproducible.sh")
        isohybrid = self.read("iso/isohybrid-reproducible.sh")

        self.assertIn("SOURCE_DATE_EPOCH", mkisofs)
        self.assertIn("-noatime", mkisofs)
        self.assertIn("-reproducible-date", mkisofs)
        self.assertIn("TZ=Europe/Berlin LC_ALL=C", mkisofs)
        self.assertIn("--id", isohybrid)
        self.assertIn("FXROUTE_REAL_MKISOFS", build)
        self.assertIn("FXROUTE_REAL_ISOHYBRID", build)

    @unittest.skipUnless(shutil.which("mkisofs"), "mkisofs is required")
    def test_mkisofs_wrapper_normalizes_rock_ridge_change_times(self):
        wrapper = ISO_DIR / "mkisofs-reproducible.sh"
        real_mkisofs = shutil.which("mkisofs")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source = temporary_path / "source"
            source.mkdir()
            (source / "file").write_text("same content\n")
            template = temporary_path / "template.iso"
            subprocess.run(
                [real_mkisofs, "-o", str(template), "-r", "-V", "TEST", str(source)],
                check=True,
                capture_output=True,
                text=True,
            )

            counter = temporary_path / "counter"
            helper = temporary_path / "fake-mkisofs"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "import sys\n"
                "template = Path(os.environ['FXROUTE_TEST_TEMPLATE'])\n"
                "counter = Path(os.environ['FXROUTE_TEST_COUNTER'])\n"
                "value = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                "counter.write_text(str(value))\n"
                "data = bytearray(template.read_bytes())\n"
                "marker = b'TF\\x1a\\x01\\x0e'\n"
                "offset = data.find(marker)\n"
                "if offset < 0:\n"
                "    raise SystemExit('test image has no Rock Ridge TF record')\n"
                "data[offset + 19 : offset + 26] = bytes((126, 1, 1, 0, 0, value, 0))\n"
                "sys.stdout.buffer.write(data)\n"
            )
            helper.chmod(0o755)

            first_output = temporary_path / "first.iso"
            environment = os.environ.copy()
            environment.update(
                {
                    "SOURCE_DATE_EPOCH": "0",
                    "FXROUTE_REAL_MKISOFS": str(helper),
                    "FXROUTE_TEST_COUNTER": str(counter),
                    "FXROUTE_TEST_TEMPLATE": str(template),
                    "LC_ALL": "C",
                    "TZ": "UTC",
                }
            )
            command = [str(wrapper), "-o", str(first_output), "-r", "-V", "TEST", str(source)]
            subprocess.run(command, check=True, env=environment, capture_output=True, text=True)
            first_image = first_output.read_bytes()

            second_output = temporary_path / "second.iso"
            command[2] = str(second_output)
            subprocess.run(command, check=True, env=environment, capture_output=True, text=True)

            self.assertEqual(first_image, second_output.read_bytes())

    @unittest.skipUnless(shutil.which("mkisofs"), "mkisofs is required")
    def test_mkisofs_wrapper_ignores_timezone_environment(self):
        wrapper = ISO_DIR / "mkisofs-reproducible.sh"
        real_mkisofs = shutil.which("mkisofs")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source = temporary_path / "source"
            source.mkdir()
            (source / "file").write_text("same content\n")
            output = temporary_path / "output.iso"
            environment = os.environ.copy()
            environment.update(
                {
                    "SOURCE_DATE_EPOCH": "0",
                    "FXROUTE_REAL_MKISOFS": real_mkisofs,
                    "LC_ALL": "C",
                    "TZ": "UTC",
                }
            )

            subprocess.run(
                [str(wrapper), "-o", str(output), "-r", "-V", "TEST", str(source)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            primary_volume_descriptor = output.read_bytes()[16 * 2048 : 17 * 2048]
            self.assertEqual(primary_volume_descriptor[813:830], b"1970010100000000\0")

    def test_isohybrid_wrapper_normalizes_efi_volume_serials(self):
        wrapper = ISO_DIR / "isohybrid-reproducible.sh"

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            helper = temporary_path / "fake-isohybrid"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                "import sys\n"
                "path = pathlib.Path(sys.argv[-1])\n"
                "data = bytearray(path.read_bytes())\n"
                "data[39:43] = b'\\x01\\x02\\x03\\x04'\n"
                "data[2048 + 39:2048 + 43] = b'\\x05\\x06\\x07\\x08'\n"
                "path.write_bytes(data)\n"
            )
            helper.chmod(0o755)

            image = temporary_path / "hybrid.iso"
            image_data = bytearray(4096)
            for offset in (0, 2048):
                image_data[offset : offset + 8] = b"\xeb\x3c\x90MTOO4049"
                image_data[offset + 39 : offset + 43] = b"\x00\x00\x00\x00"
                image_data[offset + 43 : offset + 54] = b"EFIBOOT    "
                image_data[offset + 510 : offset + 512] = b"\x55\xaa"
            image.write_bytes(image_data)

            environment = os.environ.copy()
            environment["FXROUTE_REAL_ISOHYBRID"] = str(helper)
            subprocess.run(
                [str(wrapper), str(image)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            result = image.read_bytes()
            self.assertEqual(result[39:43], b"\x78\x56\x34\x12")
            self.assertEqual(result[2048 + 39 : 2048 + 43], b"\x78\x56\x34\x12")

    def test_build_script_renders_credentials_without_committing_them(self):
        build = self.read("iso/build-leap-16-iso.sh")

        self.assertIn("FXROUTE_PASSWORD_HASH", build)
        self.assertIn("FXROUTE_SSH_PUBLIC_KEY", build)
        self.assertIn("SSH_PUBLIC_KEY=\"${SSH_PUBLIC_KEY%$'\\n'}\"", build)
        self.assertIn("__FXROUTE_PASSWORD_HASH__", self.read("iso/profiles/headless.jsonnet"))
        self.assertIn("__FXROUTE_SSH_PUBLIC_KEY__", self.read("iso/profiles/desktop.jsonnet"))

    def test_builder_accepts_newline_terminated_ssh_key_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            key_file = Path(temporary_directory) / "id_ed25519.pub"
            key_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA== test\n")
            environment = os.environ.copy()
            environment["MKMEDIA"] = "/nonexistent/mkmedia"
            result = subprocess.run(
                [
                    str(ROOT / "iso/build-leap-16-iso.sh"),
                    "--password-hash",
                    "$6$salt$hash",
                    "--ssh-public-key-file",
                    str(key_file),
                ],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("must be one line", result.stderr)
            self.assertIn("mkmedia is required", result.stderr)

    def test_profiles_use_leap_product_and_default_single_disk_layout(self):
        for profile_name in ("headless", "desktop"):
            profile_text = (PROFILE_DIR / f"{profile_name}.jsonnet").read_text()
            profile = json.loads(profile_text)
            self.assertEqual(profile["product"]["id"], "openSUSE_Leap")
            self.assertEqual(
                profile["storage"]["drives"][0]["partitions"],
                [{"generate": "default"}],
            )
            self.assertNotIn("search", profile["storage"]["drives"][0])
            self.assertEqual(profile["user"]["userName"], "fxroute")
            self.assertTrue(profile["root"]["sshPublicKey"].startswith("__FXROUTE_"))
            self.assertNotIn("password", profile["root"])
            self.assertNotIn("hashedPassword", profile["root"])
            self.assertEqual(profile["questions"]["policy"], "auto")

    def test_profiles_offer_only_the_two_fxroute_boot_entries(self):
        build = self.read("iso/build-leap-16-iso.sh")
        entries = re.findall(r'--add-entry\s+"([^"]+)"', build)

        self.assertEqual(entries, ["FXRoute Headless", "FXRoute Desktop"])
        self.assertIn("inst.auto=device:/fxroute/profiles/headless.jsonnet", build)
        self.assertIn("inst.auto=device:/fxroute/profiles/desktop.jsonnet", build)
        self.assertNotIn("devices=/dev/sr0", build)
        self.assertIn("inst.install=1", build)
        self.assertNotIn("Ubuntu", build)
        self.assertNotIn("subiquity", build)

    def test_headless_profile_has_no_graphical_stack(self):
        profile = json.loads((PROFILE_DIR / "headless.jsonnet").read_text())
        packages = set(profile["software"]["packages"])

        self.assertNotIn("plasma6-session", packages)
        self.assertNotIn("sddm-qt6", packages)
        self.assertNotIn("kde_plasma", profile["software"].get("patterns", []))

    def test_desktop_profile_requests_plasma_wayland_and_sddm(self):
        profile = json.loads((PROFILE_DIR / "desktop.jsonnet").read_text())
        packages = set(profile["software"]["packages"])

        self.assertIn("plasma6-session", packages)
        self.assertIn("sddm-qt6", packages)
        self.assertIn("qt6-wayland", packages)
        self.assertNotIn("sddm-conf", packages)
        self.assertNotIn("python313-virtualenv", packages)
        self.assertIn("kde_plasma", profile["software"]["patterns"])
        self.assertNotIn("kiosk", json.dumps(profile).lower())

    def test_installer_uses_leap_16_python_package_names(self):
        installer = self.read("install.sh")

        self.assertIn("zypper_python_package", installer)
        self.assertIn('zypper_pip_package="$(zypper_python_package pip)"', installer)
        self.assertIn('zypper_venv_package="$(zypper_python_package virtualenv)"', installer)

    def test_profiles_stage_source_archive_and_first_boot_script(self):
        for profile_name in ("headless", "desktop"):
            profile = json.loads((PROFILE_DIR / f"{profile_name}.jsonnet").read_text())
            files = {entry["destination"]: entry for entry in profile["files"]}
            self.assertEqual(
                files["/opt/fxroute-iso-source.tar"]["url"],
                "device:/fxroute/source.tar",
            )
            ssh_config = files["/etc/ssh/sshd_config.d/90-fxroute-iso.conf"]
            self.assertIn("PasswordAuthentication no", ssh_config["content"])
            self.assertIn("KbdInteractiveAuthentication no", ssh_config["content"])
            self.assertIn("PermitRootLogin prohibit-password", ssh_config["content"])
            self.assertEqual(ssh_config["permissions"], "0644")
            self.assertEqual(files["/etc/fxroute-iso-profile"]["content"], f"{profile_name}\n")
            init_script = files["/usr/local/libexec/fxroute-first-boot-install.sh"]
            self.assertEqual(
                init_script["url"],
                "device:/fxroute/scripts/first-boot-install.sh",
            )
            self.assertEqual(init_script["permissions"], "0755")

    def test_profiles_install_git_for_the_update_helper(self):
        for profile_name in ("headless", "desktop"):
            profile = json.loads((PROFILE_DIR / f"{profile_name}.jsonnet").read_text())
            self.assertIn("git", profile["software"]["packages"])

    def test_profiles_stage_the_build_commit_marker(self):
        for profile_name in ("headless", "desktop"):
            profile = json.loads((PROFILE_DIR / f"{profile_name}.jsonnet").read_text())
            files = {entry["destination"]: entry for entry in profile["files"]}
            marker = files["/opt/fxroute-iso-build-commit"]
            self.assertEqual(marker["url"], "device:/fxroute/build-commit")
            self.assertEqual(marker["permissions"], "0644")

    def test_builder_records_the_built_commit_for_update_setup(self):
        build = self.read("iso/build-leap-16-iso.sh")

        self.assertIn(
            'git -C "$ROOT_DIR" rev-parse HEAD > "$STAGE_DIR/fxroute/build-commit"',
            build,
        )

    def test_builder_refuses_unpushed_source_commits(self):
        build = self.read("iso/build-leap-16-iso.sh")

        self.assertIn('git -C "$ROOT_DIR" merge-base --is-ancestor HEAD origin/main', build)
        self.assertIn("FXROUTE_ISO_ALLOW_UNPUSHED", build)
        self.assertIn("push the ISO source commit first", build)

    def test_first_boot_prepares_git_based_updates(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn("https://github.com/CobbyCode/fxroute.git", script)
        self.assertIn("enable_git_updates", script)
        self.assertIn("/opt/fxroute-iso-build-commit", script)
        self.assertIn("fetch -q --no-tags origin", script)
        self.assertIn("http.lowSpeedLimit=1000", script)
        self.assertIn("for attempt in 1 2 3; do", script)
        self.assertIn("checkout -q -f -B main", script)
        self.assertIn("config branch.main.remote origin", script)
        # Git runs as the target user: the install tree is fxroute-owned and
        # git refuses repositories with a different owner (dubious ownership).
        self.assertIn('runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home"', script)
        # Unpushed dev/test ISOs must never overwrite the installed tree with
        # an older checkout; they record a local snapshot commit instead.
        self.assertIn("reset -q --soft origin/main", script)
        self.assertIn("FXRoute ISO install snapshot", script)
        # The git update setup is best effort and must never fail the install.
        self.assertIn("updates stay disabled", script)

    def test_verifier_checks_the_git_update_path(self):
        runner = self.read("iso/test-leap-16-iso.sh")

        self.assertIn("test -d /home/fxroute/fxroute/.git", runner)
        self.assertIn("update_fxroute.sh --check", runner)
        self.assertIn("reconciliation is incomplete", runner)

    def test_profiles_install_and_enable_the_first_boot_service(self):
        for profile_name in ("headless", "desktop"):
            profile = json.loads((PROFILE_DIR / f"{profile_name}.jsonnet").read_text())
            self.assertNotIn("agama-scripts", profile["software"]["packages"])
            files = {entry["destination"]: entry for entry in profile["files"]}
            service = files["/etc/systemd/system/fxroute-first-boot.service"]
            self.assertIn("ExecStart=/usr/local/libexec/fxroute-first-boot-install.sh", service["content"])
            self.assertEqual(service["permissions"], "0644")
            self.assertNotIn("init", profile["scripts"])
            post_scripts = profile["scripts"]["post"]
            self.assertEqual(post_scripts[0]["chroot"], True)
            self.assertIn("multi-user.target.wants", post_scripts[0]["content"])

    def test_first_boot_script_uses_the_existing_installer(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn('SOURCE_ARCHIVE="/opt/fxroute-iso-source.tar"', script)
        self.assertIn('"$SOURCE_DIR/install.sh"', script)
        self.assertIn('export HOME="$fxroute_home"', script)
        self.assertIn("--source", script)
        self.assertIn("--providers none", script)
        self.assertIn('COMPLETE_MARKER="$STATE_DIR/install-complete"', script)
        self.assertIn("network-online.target", script)
        self.assertIn("sddm.conf.d/10-fxroute-autologin.conf", script)
        self.assertIn("plasmawayland", script)

    def test_first_boot_restricts_sshd_to_key_authentication(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn("/etc/ssh/sshd_config.d/90-fxroute-iso.conf", script)
        self.assertIn("PasswordAuthentication no", script)
        self.assertIn("KbdInteractiveAuthentication no", script)
        self.assertIn("PermitRootLogin prohibit-password", script)
        self.assertIn("install -d -m 755 /run/sshd", script)
        self.assertIn("ssh-keygen -A", script)
        self.assertLess(
            script.index("PasswordAuthentication no"),
            script.index("systemctl enable --now sshd.service"),
        )
        self.assertLess(script.index("install -d -m 755 /run/sshd"), script.index("sshd -t"))
        self.assertLess(script.index("ssh-keygen -A"), script.index("sshd -t"))

    def test_desktop_first_boot_uses_official_chrome_repository_and_not_kiosk(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn("https://dl.google.com/linux/chrome/rpm/stable/x86_64", script)
        self.assertIn("https://dl.google.com/linux/linux_signing_key.pub", script)
        self.assertIn("rpm --import", script)
        self.assertIn("54dea5f6c2a26091578cf52a999cebc6b64df478d37ad4dce96376b711e3b27c", script)
        self.assertIn("gpgkey=", script)
        self.assertIn("google-chrome-stable", script)
        self.assertIn("--new-window http://127.0.0.1:8000", script)
        self.assertNotIn("--kiosk", script)

    def test_first_boot_can_retry_after_a_failed_attempt(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn('rm -f -- "$FAILED_MARKER"', script)
        self.assertIn('IN_PROGRESS_MARKER="$STATE_DIR/install-in-progress"', script)
        self.assertIn('touch "$IN_PROGRESS_MARKER"', script)
        self.assertIn('rm -f -- "$IN_PROGRESS_MARKER"', script)
        self.assertIn('if [[ -f "$FAILED_MARKER" ]]; then', script)
        self.assertIn("reset_incomplete_install", script)
        self.assertIn('local target="$fxroute_home/fxroute"', script)
        self.assertIn('rm -rf -- "$target"', script)
        self.assertIn('rm -rf -- "$root_state_dir"', script)

    def test_desktop_first_boot_starts_sddm_without_ordering_deadlock(self):
        script = self.read("iso/scripts/first-boot-install.sh")

        self.assertIn("systemctl enable --force sddm.service", script)
        self.assertIn("systemctl start --no-block sddm.service", script)
        self.assertNotIn("systemctl enable --force --now sddm.service", script)
        self.assertIn("displaymanager_config", script)

    def test_first_boot_operations_have_bounded_network_requests(self):
        script = self.read("iso/scripts/first-boot-install.sh")
        service_profiles = "".join(
            self.read(f"iso/profiles/{profile}.jsonnet")
            for profile in ("headless", "desktop")
        )
        self.assertIn("--connect-timeout 10 --max-time 120", script)
        self.assertIn("--connect-timeout 5 --max-time 30", script)
        self.assertIn("TimeoutStartSec=2h", service_profiles)

    def test_qemu_test_runner_covers_fresh_headless_and_desktop_guests(self):
        runner = self.read("iso/test-leap-16-iso.sh")

        self.assertIn('headless|desktop', runner)
        self.assertIn("qemu-img create", runner)
        self.assertIn("-cdrom", runner)
        self.assertIn("select_boot_entry", runner)
        self.assertIn("screendump", runner)
        self.assertIn("grub_menu_ready", runner)
        self.assertNotIn("sleep 3", runner)
        self.assertIn("sendkey", runner)
        self.assertIn("find_free_port", runner)
        self.assertIn("wait_for_qemu_processes", runner)
        self.assertIn("install-failed", runner)
        self.assertIn("--connect-timeout", runner)
        self.assertIn("--max-time", runner)
        self.assertIn("qemu.log", runner)
        self.assertIn("install-complete", runner)
        self.assertIn("plasma6-session", runner)
        self.assertIn("systemctl reboot", runner)
        self.assertIn("/proc/sys/kernel/random/boot_id", runner)
        self.assertIn("systemctl is-active sddm.service", runner)
        self.assertIn("display-manager.service", runner)
        self.assertIn('loginctl show-session "$candidate" -p Type --value', runner)
        self.assertIn('= wayland', runner)
        self.assertIn("gpgkey=https://dl.google.com/linux/linux_signing_key.pub", runner)
        self.assertIn("90-fxroute-iso.conf", runner)
        self.assertIn("PasswordAuthentication no", runner)
        self.assertIn("PermitRootLogin prohibit-password", runner)
        self.assertIn("sshd -T", runner)
        self.assertIn("getent shadow root", runner)
        self.assertIn("loginctl", runner)
        self.assertIn("127.0.0.1:8000", runner)
        self.assertIn("fxroute_dsp_sink", runner)
        self.assertIn("/api/status", runner)

    def test_grub_screendump_uses_the_configured_temp_directory(self):
        runner = self.read("iso/test-leap-16-iso.sh")

        self.assertIn(
            'tempfile.mkstemp(\n    prefix="fxroute-iso-grub-", suffix=".ppm"\n)',
            runner,
        )
        self.assertIn("def hmp_quote_path(path):", runner)
        self.assertIn(
            'monitor_command(f"screendump {hmp_quote_path(screen_path)}")',
            runner,
        )
        self.assertNotIn('dir="/tmp"', runner)

    def test_desktop_verifier_waits_for_delayed_chrome_autostart(self):
        runner = self.read("iso/test-leap-16-iso.sh")

        self.assertIn("for _ in $(seq 1 60); do", runner)
        self.assertIn(
            "if pgrep -u fxroute -f '(^|/)chrome( |$)' >/dev/null &&",
            runner,
        )
        self.assertIn(
            "pgrep -u fxroute -f '127\\.0\\.0\\.1:8000' >/dev/null",
            runner,
        )

    def test_qemu_verifier_consumes_piped_output_before_matching(self):
        runner = self.read("iso/test-leap-16-iso.sh")

        self.assertIn(
            "sshd -T | grep -Fx 'passwordauthentication no' >/dev/null",
            runner,
        )
        self.assertIn(
            "sshd -T | grep -Fx 'kbdinteractiveauthentication no' >/dev/null",
            runner,
        )
        self.assertIn(
            "sshd -T | grep -E '^permitrootlogin (prohibit-password|without-password)$' >/dev/null",
            runner,
        )
        self.assertNotIn("sshd -T | grep -Fxq", runner)
        self.assertNotIn("sshd -T | grep -Eq", runner)
        self.assertNotIn(
            "pactl list sinks short | awk '{print $2}' | grep -Fxq",
            runner,
        )

    def test_iso_documentation_describes_build_credentials_and_wlan_limit(self):
        docs = self.read("docs/INSTALL-ISO.md")

        self.assertIn("Leap-16.0-offline-installer-x86_64.install.iso", docs)
        self.assertIn("FXROUTE_PASSWORD_HASH", docs)
        self.assertIn("FXROUTE_SSH_PUBLIC_KEY", docs)
        self.assertIn("WLAN", docs)
        self.assertIn("Spotify Desktop", docs)
        self.assertIn("spotifyd", docs)
        self.assertIn("SOURCE_DATE_EPOCH", docs)
        self.assertIn("byte-identical", docs)
        self.assertIn("agama config validate --local", docs)
        self.assertIn("if=pflash", docs)
        self.assertIn("ovmf-x86_64-4m-code.bin", docs)
        self.assertIn("Do not publish an ISO built with test credentials", docs)
        self.assertIn("PasswordAuthentication no", docs)
        self.assertIn("PermitRootLogin prohibit-password", docs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
