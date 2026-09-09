#!/usr/bin/env python3
"""qbzd download failures must fail controlled, never abort the installer.

Contract (parity with install_spotifyd_binary): a failed curl download, a
checksum mismatch, a failed extract and a missing binary inside the archive
each end with ``warn`` + ``return 1``. The harness stays alive, no binary
is installed, and the specific warning names the failed stage.

The pre-fix qbzd path ran bare ``curl``/``sha256sum``/``tar`` (``set -e``
aborts the whole installer on failure) and ``die`` on a missing binary.
``die`` here is production-faithful (``exit 1``), so an uncontrolled abort
kills the harness subshell without the warning marker and fails these
tests; a controlled ``return 1`` prints the warning and lets the harness
continue.
"""

import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


INSTALL_TEXT = INSTALL_SH.read_text()
ARCH_HELPER = extract_function(INSTALL_TEXT, "qbzd_arch_for_host")
PATH_HELPER = extract_function(INSTALL_TEXT, "qbzd_binary_path")
INSTALL_QBZD = extract_function(INSTALL_TEXT, "install_qbzd_binary")


def run_harness(*, fixture_mode: str, work: Path) -> subprocess.CompletedProcess:
    """Run install_qbzd_binary in a subshell; return the harness result.

    fixture_mode selects the stubbed failure:
    - "checksum-mismatch": real download + real sha256sum (mismatches the
      hardcoded checksum), i.e. a tampered archive;
    - "curl-fails": the curl stub fails, i.e. no network / 404;
    - "missing-binary": sha256sum is stubbed to pass so the extract runs,
      but the archive contains no qbzd file.
    """
    fixture_archive = work / "fixture.tar.gz"
    if fixture_mode == "missing-binary":
        readme = work / "readme.txt"
        readme.write_text("no binary here\n")
        subprocess.run(
            ["tar", "-czf", str(fixture_archive), "-C", str(work), "readme.txt"],
            check=True,
        )
    else:
        source_dir = work / "source"
        source_dir.mkdir(exist_ok=True)
        source_binary = source_dir / "qbzd"
        source_binary.write_text("portable qbzd\n")
        source_binary.chmod(0o755)
        subprocess.run(
            ["tar", "-czf", str(fixture_archive), "-C", str(source_dir), "qbzd"],
            check=True,
        )
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    target_home = work / "home"
    harness = f"""
set -Eeuo pipefail
{ARCH_HELPER}
{PATH_HELPER}
{INSTALL_QBZD}
run_cmd() {{
  if [[ "${{1:-}}" == "curl" ]]; then
    if [[ "$CURL_FAIL" == 1 ]]; then return 1; fi
    local dest="" prev=""
    for arg in "$@"; do
      if [[ "$prev" == "-o" ]]; then dest="$arg"; fi
      prev="$arg"
    done
    cp "$FIXTURE_ARCH" "$dest"
    return 0
  fi
  "$@"
}}
run_as_target_user() {{ "$@"; }}
pass() {{ :; }}
warn() {{ printf 'qbzd-warn: %s\\n' "$*" >&2; }}
die() {{ printf '[fxroute][error] %s\\n' "$*" >&2; exit 1; }}
SHA256SUM_STUB={ "true" if fixture_mode == "missing-binary" else "false" }
if [[ "$SHA256SUM_STUB" == "true" ]]; then
  sha256sum() {{ return 0; }}
fi
export PATH="{bin_dir}:$PATH"
HOME={target_home}
HOST_ARCH=x86_64
QBZD_VERSION=1.2.3
CURL_FAIL={ "1" if fixture_mode == "curl-fails" else "0" }
FIXTURE_ARCH={fixture_archive}
QBZD_INSTALLED_BY_FXROUTE=0
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH=''
QBZD_BINARY_SHA256=''
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
( install_qbzd_binary ) || rc=$?
printf 'harness-rc=%s\\n' "$rc"
test ! -e "$HOME/.local/bin/qbzd" && printf 'no-binary-installed\\n'
printf 'harness-alive\\n'
"""
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


class QbzdDownloadFailureTests(unittest.TestCase):
    def test_checksum_mismatch_fails_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_harness(fixture_mode="checksum-mismatch", work=Path(td))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harness-rc=1", result.stdout)
        self.assertIn("harness-alive", result.stdout)
        self.assertIn("no-binary-installed", result.stdout)
        self.assertIn(
            "checksum mismatch",
            result.stderr,
            "a tampered archive must warn and return 1, not abort the installer",
        )

    def test_download_failure_fails_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_harness(fixture_mode="curl-fails", work=Path(td))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harness-rc=1", result.stdout)
        self.assertIn("harness-alive", result.stdout)
        self.assertIn("no-binary-installed", result.stdout)
        self.assertIn(
            "could not be downloaded",
            result.stderr,
            "a failed download must warn and return 1, not abort the installer",
        )

    def test_missing_binary_fails_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_harness(fixture_mode="missing-binary", work=Path(td))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harness-rc=1", result.stdout)
        self.assertIn("harness-alive", result.stdout)
        self.assertIn("no-binary-installed", result.stdout)
        self.assertIn(
            "did not contain a binary",
            result.stderr,
            "an archive without qbzd must warn and return 1, not die",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
