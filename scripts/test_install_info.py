#!/usr/bin/env python3
"""Behavior tests for the REFACTOR-009 extraction:

- install_info.read_version_file
- install_info.read_build_id
- install_info.read_install_config
- install_info.configured_service_name

plus wrapper parity against main._read_version_file, main._read_build_id
and main._configured_service_name.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import install_info


def _git_result(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


class ReadVersionFileTests(unittest.TestCase):
    def test_existing_version_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_version_file(), "1.2.3")

    def test_existing_version_whitespace_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("  1.2.3  \n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_version_file(), "1.2.3")

    def test_empty_version_file(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_version_file(), "")

    def test_missing_version_file(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(install_info, "BASE_DIR", Path(td)):
                self.assertEqual(install_info.read_version_file(), "")


class ReadBuildIdTests(unittest.TestCase):
    def test_build_id_present_has_priority(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            (base / "BUILD_ID").write_text("abc123\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_build_id(), "1.2.3 abc123")

    def test_build_id_whitespace_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            (base / "BUILD_ID").write_text("  abc123  \n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_build_id(), "1.2.3 abc123")

    def test_empty_build_id_falls_back_to_git(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            (base / "BUILD_ID").write_text("", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", return_value=_git_result(0, "deadbeef\n")
            ) as run:
                self.assertEqual(install_info.read_build_id(), "1.2.3 commit=deadbeef")
                run.assert_called_once()
                args = run.call_args.args[0]
                self.assertEqual(args, ["git", "-C", str(base), "rev-parse", "--short", "HEAD"])
                self.assertEqual(run.call_args.kwargs["timeout"], 1.5)

    def test_missing_build_id_falls_back_to_git(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", return_value=_git_result(0, "deadbeef\n")
            ):
                self.assertEqual(install_info.read_build_id(), "1.2.3 commit=deadbeef")

    def test_git_failure_commit_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", return_value=_git_result(1, "")
            ):
                self.assertEqual(install_info.read_build_id(), "1.2.3 commit=unknown")

    def test_git_exception_commit_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", side_effect=OSError("git missing")
            ):
                self.assertEqual(install_info.read_build_id(), "1.2.3 commit=unknown")

    def test_git_timeout_commit_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1.5)
            ):
                self.assertEqual(install_info.read_build_id(), "1.2.3 commit=unknown")

    def test_missing_version_unknown_version_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", return_value=_git_result(0, "deadbeef\n")
            ):
                self.assertEqual(install_info.read_build_id(), "unknown-version commit=deadbeef")

    def test_reads_anew_each_call_no_cache(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            version_file = base / "VERSION"
            version_file.write_text("1.0.0\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(install_info.read_version_file(), "1.0.0")
                version_file.write_text("2.0.0\n", encoding="utf-8")
                self.assertEqual(install_info.read_version_file(), "2.0.0")


class ReadInstallConfigTests(unittest.TestCase):
    def test_comments_blank_and_invalid_lines_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text(
                "# comment\n"
                "\n"
                "   \n"
                "KEY=value\n"
                "noequals\n"
                "  # indented comment\n",
                encoding="utf-8",
            )
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {"KEY": "value"})

    def test_multiple_equals_split_at_first(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("URL=https://host/path?a=b\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {"URL": "https://host/path?a=b"})

    def test_whitespace_trimmed(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("  KEY  =  value  \n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {"KEY": "value"})

    def test_duplicate_keys_last_wins(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("KEY=first\nKEY=second\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {"KEY": "second"})

    def test_missing_config_file_empty_result(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "nope.env"
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {})

    def test_reads_anew_each_call_no_cache(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("KEY=one\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.read_install_config(), {"KEY": "one"})
                config.write_text("KEY=two\n", encoding="utf-8")
                self.assertEqual(install_info.read_install_config(), {"KEY": "two"})


class ConfiguredServiceNameTests(unittest.TestCase):
    def test_configured_service_name(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("FXROUTE_SERVICE_NAME=myfx\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.configured_service_name(), "myfx")

    def test_configured_service_name_whitespace_trimmed(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("FXROUTE_SERVICE_NAME =  myfx  \n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.configured_service_name(), "myfx")

    def test_empty_service_name_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("FXROUTE_SERVICE_NAME=\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.configured_service_name(), "fxroute")

    def test_missing_service_name_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("OTHER=value\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.configured_service_name(), "fxroute")

    def test_missing_config_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "nope.env"
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(install_info.configured_service_name(), "fxroute")


class WrapperParityTests(unittest.TestCase):
    def test_read_version_file_parity(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(main._read_version_file(), install_info.read_version_file())

    def test_read_build_id_parity(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            (base / "BUILD_ID").write_text("abc123\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base):
                self.assertEqual(main._read_build_id(), install_info.read_build_id())

    def test_read_build_id_parity_git_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with patch.object(install_info, "BASE_DIR", base), patch.object(
                install_info.subprocess, "run", return_value=_git_result(0, "deadbeef\n")
            ):
                self.assertEqual(main._read_build_id(), install_info.read_build_id())

    def test_configured_service_name_parity(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "install-config.env"
            config.write_text("FXROUTE_SERVICE_NAME=myfx\n", encoding="utf-8")
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(main._configured_service_name(), install_info.configured_service_name())

    def test_configured_service_name_parity_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "nope.env"
            with patch.object(install_info, "INSTALL_CONFIG_FILE", config):
                self.assertEqual(main._configured_service_name(), install_info.configured_service_name())


if __name__ == "__main__":
    unittest.main()
