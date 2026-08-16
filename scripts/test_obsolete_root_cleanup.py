#!/usr/bin/env python3
"""Migration cleanup tests for the package reorganisation.

Verifies that scripts/fxroute_obsolete_root_cleanup.py:

- removes exactly the obsolete pre-package root modules from a mock
  existing install and nothing else;
- leaves current package files, .env, .venv, and user/runtime files
  untouched;
- is idempotent (running twice is harmless);
- skips removal when the new package layout is not fully installed;
- the manifest is authoritative: it covers every root module renamed into
  a package by the migration commits and none of the eight intentionally
  remaining root Python modules.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import fxroute_obsolete_root_cleanup as cleanup

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "fxroute_obsolete_root_cleanup.py"

REMAINING_ROOT_MODULES = (
    "config.py",
    "downloader.py",
    "install_info.py",
    "main.py",
    "models.py",
    "safe_http.py",
    "uploads.py",
    "zip_album.py",
)

# Representative obsolete root modules for the mock install (a subset is
# enough for behavior; completeness of the manifest is checked separately).
MOCK_OBSOLETE = (
    "samplerate.py",
    "player.py",
    "dsp_runtime.py",
    "library.py",
    "measurement.py",
    "stations.py",
    "autosub.py",
    "volume_contract.py",
)

MOCK_PACKAGE_FILES = (
    "playback/player.py",
    "dsp/runtime.py",
    "library/core.py",
    "radio/stations.py",
    "audio/samplerate/__init__.py",
    "measurement/store.py",
)

MOCK_PROTECTED = (
    ".env",
    ".venv/bin/python3",
    "media/cache/covers/x.png",
    "presets/Neutral.json",
    "measurements/session.json",
    "runtime-state/state.bin",
)


def _make_mock_install(root: Path, *, with_packages: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in MOCK_OBSOLETE:
        (root / name).write_text(f"# obsolete {name}\n", encoding="utf-8")
    if with_packages:
        for rel in MOCK_PACKAGE_FILES:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# package {rel}\n", encoding="utf-8")
    for rel in MOCK_PROTECTED:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# protected {rel}\n", encoding="utf-8")


class ObsoleteRootCleanupBehaviorTests(unittest.TestCase):
    def test_migration_removes_obsolete_keeps_packages_and_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "fxroute"
            _make_mock_install(install)
            result = subprocess.run(
                ["python3", str(HELPER), "--root", str(install)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in MOCK_OBSOLETE:
                self.assertFalse((install / name).exists(), f"{name} should be removed")
            for rel in MOCK_PACKAGE_FILES:
                self.assertTrue((install / rel).is_file(), f"{rel} should remain")
            for rel in MOCK_PROTECTED:
                self.assertTrue((install / rel).is_file(), f"{rel} should remain")

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "fxroute"
            _make_mock_install(install)
            for _ in range(2):
                result = subprocess.run(
                    ["python3", str(HELPER), "--root", str(install)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            for name in MOCK_OBSOLETE:
                self.assertFalse((install / name).exists())
            for rel in MOCK_PACKAGE_FILES + MOCK_PROTECTED:
                self.assertTrue((install / rel).is_file(), f"{rel} should remain")

    def test_skips_when_new_layout_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "fxroute"
            _make_mock_install(install, with_packages=False)
            result = subprocess.run(
                ["python3", str(HELPER), "--root", str(install)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("not fully installed", result.stderr)
            for name in MOCK_OBSOLETE:
                self.assertTrue((install / name).exists(), f"{name} must be kept")
            for rel in MOCK_PROTECTED:
                self.assertTrue((install / rel).is_file(), f"{rel} should remain")

    def test_helper_removes_nothing_without_packages_on_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "fxroute"
            install.mkdir()
            result = subprocess.run(
                ["python3", str(HELPER), "--root", str(install)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(install.iterdir()), [])


class ObsoleteRootManifestCompletenessTests(unittest.TestCase):
    """The manifest must match the actual root-module renames exactly."""

    @classmethod
    def setUpClass(cls):
        # Derive the authoritative obsolete set from the migration commits:
        # every root-level (no '/') Python source renamed by the commit set.
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "log",
                "--format=%h %s",
                "--diff-filter=R",
                "-M",
                "--name-status",
                "00a4d86^..ff6907d",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        renamed_sources = set()
        for line in proc.stdout.splitlines():
            if "\t" in line and line.split("\t", 1)[0].startswith("R"):
                _, source, _ = line.split("\t", 2)
                if "/" not in source and source.endswith(".py"):
                    renamed_sources.add(source)
        # The extraction commits (before the package moves) created the
        # measurement_*.py modules as new files at root; the renames above
        # cover them when moved under measurement/.  Verify the union with
        # the committed move list by checking both directions below.
        #
        # Post-migration moves of root modules into packages are added
        # explicitly (each must come with a matching package rename):
        # power.py -> audio/power.py.
        cls.renamed_sources = renamed_sources | {"power.py"}

    def test_manifest_contains_all_renamed_root_modules(self):
        missing = sorted(self.renamed_sources - set(cleanup.OBSOLETE_ROOT_MODULES))
        self.assertEqual(missing, [], f"manifest missing renamed root modules: {missing}")

    def test_manifest_has_no_unknown_entries(self):
        extra = sorted(set(cleanup.OBSOLETE_ROOT_MODULES) - self.renamed_sources)
        self.assertEqual(extra, [], f"manifest has entries without a rename: {extra}")

    def test_manifest_excludes_remaining_root_modules(self):
        overlap = sorted(set(cleanup.OBSOLETE_ROOT_MODULES) & set(REMAINING_ROOT_MODULES))
        self.assertEqual(overlap, [], f"manifest must not list remaining modules: {overlap}")

    def test_remaining_root_modules_are_the_expected_eight(self):
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "*.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        root_modules = sorted(name for name in tracked if "/" not in name)
        self.assertEqual(root_modules, sorted(REMAINING_ROOT_MODULES))


if __name__ == "__main__":
    unittest.main()
