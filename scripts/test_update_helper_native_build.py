#!/usr/bin/env python3
"""Static contract for native-helper rebuild input freshness."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeHelperBuildTests(unittest.TestCase):
    def test_update_checks_build_script_and_c_source(self):
        script = (ROOT / "scripts/update_fxroute.sh").read_text()
        self.assertIn('local source_file="$REPO_PATH/pipewire_stage1/fxroute_21_passthrough.c"', script)
        self.assertIn(
            '[[ "$build_script" -nt "$binary" || "$source_file" -nt "$binary" ]]',
            script,
        )

    def test_incomplete_deployment_is_reconciled_at_current_head(self):
        script = (ROOT / "scripts/update_fxroute.sh").read_text()
        self.assertIn("reconciliation_marker_matches_head()", script)
        self.assertIn("mark_reconciliation_complete()", script)
        self.assertIn(
            "if reconciliation_marker_matches_head; then",
            script,
        )
        self.assertIn(
            'if [[ "$MODE" == "check" ]]; then\n      log "Update available."',
            script,
        )
        self.assertIn("retrying reconciliation", script)

    def test_failed_native_build_does_not_mark_reconciliation_complete(self):
        script = (ROOT / "scripts/update_fxroute.sh").read_text()
        self.assertIn("NATIVE_HELPER_BUILD_OK=0", script)
        self.assertIn('if [[ "$NATIVE_HELPER_BUILD_OK" == "1" ]]; then', script)
        self.assertIn("Reconciliation remains incomplete", script)

    def test_same_head_reconciliation_requests_api_restart(self):
        main_source = (ROOT / "main.py").read_text()
        self.assertIn(
            '"Checkout is current, but the deployment was not completed; retrying reconciliation."',
            main_source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
