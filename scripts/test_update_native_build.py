#!/usr/bin/env python3
"""Native build outcomes must propagate through reconciliation to the API."""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_restore_untracked_backup import SCRIPT_TEXT, extract_function, make_repo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def update_request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "POST", "path": "/api/system/update",
        "scheme": "http", "server": ("testserver", 80),
        "headers": [], "query_string": b"",
    })


def make_update_harness(root: Path, work: Path, *, build_succeeds: bool) -> Path:
    source = work / "native_dsp"
    source.mkdir()
    if build_succeeds:
        build = '#!/bin/bash\nmkdir -p native_dsp/build\nprintf "#!/bin/sh\\n" > native_dsp/build/fxroute-dsp\nchmod +x native_dsp/build/fxroute-dsp\n'
    else:
        build = "#!/bin/bash\nexit 17\n"
    (source / "build.sh").write_text(build)
    with (work / ".gitignore").open("a") as ignore:
        ignore.write(".venv/\nnative_dsp/build/\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "native fixture"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"], check=True)

    helpers = "\n".join(extract_function(SCRIPT_TEXT, name) for name in (
        "setup_repo", "version_at", "git_short", "git_remote_ref",
        "reconciliation_marker_matches_head", "mark_reconciliation_complete",
        "build_native_dsp_if_needed", "reconcile_checkout", "main",
    ))
    harness = root / "update-test.sh"
    harness.write_text(f'''#!/bin/bash
set -Eeuo pipefail
REPO_PATH="$FXROUTE_REPO_PATH"
SERVICE_NAME=fxroute-test
MODE=update
log() {{ printf '[fxroute-update] %s\\n' "$*"; }}
die() {{ printf '[fxroute-update][error] %s\\n' "$*" >&2; exit 1; }}
resolve_repo_path() {{ printf '%s\\n' "$FXROUTE_REPO_PATH"; }}
install_dependencies_if_needed() {{ :; }}
run_production_build() {{ :; }}
cleanup_obsolete_root_modules() {{ :; }}
restart_service_if_needed() {{ printf 'restart\\n' > "$REPO_PATH/restart-called"; }}
{helpers}
main
''')
    harness.chmod(0o755)
    return harness


def run_update(script: Path, work: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script)], env={**os.environ, "FXROUTE_REPO_PATH": str(work)},
        capture_output=True, text=True,
    )


class NativeBuildUpdateTests(unittest.TestCase):
    def test_failed_native_build_fails_reconciliation_before_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work = make_repo(root)
            script = make_update_harness(root, work, build_succeeds=False)

            result = run_update(script, work)

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("native DSP engine build failed", result.stdout)
            self.assertFalse((work / "restart-called").exists())
            self.assertFalse((work / ".venv/.fxroute-reconciled-commit").exists())
            self.assertNotIn("Update reconciliation completed", result.stdout)

    def test_successful_native_build_reconciles_and_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work = make_repo(root)
            script = make_update_harness(root, work, build_succeeds=True)

            result = run_update(script, work)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((work / "native_dsp/build/fxroute-dsp").stat().st_mode & 0o111)
            self.assertEqual((work / "restart-called").read_text(), "restart\n")
            self.assertEqual(
                (work / ".venv/.fxroute-reconciled-commit").read_text().strip(),
                subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip(),
            )

    def test_failed_native_build_is_api_failure_without_deferred_restart(self):
        import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work = make_repo(root)
            script = make_update_harness(root, work, build_succeeds=False)
            with mock.patch.object(main, "UPDATE_SCRIPT", script), \
                    mock.patch.dict(os.environ, {"FXROUTE_REPO_PATH": str(work)}):
                response = asyncio.run(main.system_update(update_request()))

            self.assertFalse(response["ok"])
            self.assertNotEqual(response["returncode"], 0)
            self.assertFalse(response["restart_scheduled"])
            self.assertFalse((work / "restart-called").exists())

    def test_successful_native_build_keeps_api_deferred_restart(self):
        import main

        async def fake_restart(service_name: str) -> None:
            pass

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work = make_repo(root)
            script = make_update_harness(root, work, build_succeeds=True)
            with mock.patch.object(main, "UPDATE_SCRIPT", script), \
                    mock.patch.object(main, "_restart_fxroute_service_after_response", fake_restart), \
                    mock.patch.dict(os.environ, {"FXROUTE_REPO_PATH": str(work)}):
                response = asyncio.run(main.system_update(update_request()))

            self.assertTrue(response["ok"])
            self.assertEqual(response["returncode"], 0)
            self.assertTrue(response["restart_scheduled"])
            self.assertTrue((work / "native_dsp/build/fxroute-dsp").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
