#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Shared runner for pytest-style native DSP test modules.
#
# Native DSP suites are executed directly with python3 (no pytest
# dependency).  This runner discovers every test_* callable in the module,
# runs it with an isolated temporary directory when it declares the
# tmp_path parameter (pytest-style) or with no arguments otherwise, and
# returns a nonzero exit code when any test failed or when no tests ran, so
# a file can never be counted as passed without its tests actually running.

import inspect
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict


def run_pytest_style_module(module_globals: Dict[str, Any]) -> int:
    tests = sorted(
        (name, function)
        for name, function in module_globals.items()
        if name.startswith("test_") and callable(function)
    )
    if not tests:
        print("no test_* functions found in module; refusing to pass", file=sys.stderr)
        return 2
    failures = 0
    reasons = []
    for name, function in tests:
        try:
            signature = inspect.signature(function)
            if "tmp_path" in signature.parameters:
                with tempfile.TemporaryDirectory(prefix=f"fxroute-{name}-") as directory:
                    function(Path(directory))
            else:
                function()
            print(f"PASS  {name}")
        except Exception as error:
            failures += 1
            reasons.append(f"{name}: {type(error).__name__}: {error}".strip())
            print(f"FAIL  {name}")
            traceback.print_exc()
    if failures:
        # Repeat the reasons after the per-test lines: suite logs are often
        # captured as a tail, which otherwise loses the traceback above.
        print("failure summary:", file=sys.stderr)
        for reason in reasons:
            print(f"  {' '.join(reason.split())}", file=sys.stderr)
        print(f"{failures} of {len(tests)} native DSP tests failed", file=sys.stderr)
        return 1
    print(f"native DSP tests passed: {len(tests)}")
    return 0
