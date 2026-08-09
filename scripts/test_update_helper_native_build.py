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
        self.assertIn("deployment_marker_matches_head()", script)
        self.assertIn("mark_deployment_complete()", script)
        self.assertIn(
            'if [[ "$MODE" == "check" ]] || deployment_marker_matches_head; then',
            script,
        )
        self.assertIn("retrying reconciliation", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
