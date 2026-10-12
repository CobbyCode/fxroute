#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests: the service runs OpenBLAS without worker threads.

uvloop starts child processes with fork().  OpenBLAS stops its worker threads
in every fork, and a threaded BLAS call running at that moment never returns:
a sweep analysis at 88.2 kHz and above (timing anchors longer than OpenBLAS's
10000-sample threading threshold) stayed in "Processing measurement…" forever.

main.py therefore pins OpenBLAS to one thread before numpy loads it.  Covered:

- the assignment precedes every import except ``os``
- importing main yields the limit with and without a value in the environment
- the numpy-bundled OpenBLAS then really runs one thread (skipped where numpy
  links another BLAS)
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIABLE = "OPENBLAS_NUM_THREADS"

# Imports main like the service does and reports what OpenBLAS ended up with.
CHILD = r"""
import ctypes, glob, json, os, sys
import main
import numpy
threads = None
pattern = os.path.join(os.path.dirname(numpy.__file__), os.pardir, "numpy.libs", "libscipy_openblas*.so")
for path in glob.glob(pattern):
    library = ctypes.CDLL(path)
    for name in ("scipy_openblas_get_num_threads64_", "scipy_openblas_get_num_threads"):
        if hasattr(library, name):
            threads = getattr(library, name)()
            break
print("RESULT " + json.dumps({"env": os.environ.get("OPENBLAS_NUM_THREADS"), "openblas_threads": threads}))
"""


def _is_limit_assignment(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    return (
        isinstance(target, ast.Subscript)
        and ast.unparse(target.value) == "os.environ"
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == VARIABLE
        and isinstance(node.value, ast.Constant)
        and node.value.value == "1"
    )


def _import_main(preset: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="fxroute-blas-limit-") as sandbox:
        env = dict(os.environ)
        env.pop(VARIABLE, None)
        if preset is not None:
            env[VARIABLE] = preset
        for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
            env[name] = str(Path(sandbox) / name.lower())
        proc = subprocess.run(
            [sys.executable, "-c", CHILD], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError(f"importing main failed (exit {proc.returncode}): {proc.stderr[-800:]}")
    return json.loads(lines[-1][len("RESULT "):])


class LimitPlacementTests(unittest.TestCase):
    def test_limit_precedes_every_import_but_os(self):
        body = ast.parse((ROOT / "main.py").read_text()).body
        position = next((index for index, node in enumerate(body) if _is_limit_assignment(node)), None)
        self.assertIsNotNone(position, f"main.py does not set {VARIABLE} to 1")
        imported: list[str] = []
        for node in body[:position]:
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(str(node.module))
        self.assertEqual(imported, ["os"], "an import ahead of the limit could load numpy first")


class LimitInEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.unset = _import_main(None)
        cls.preset = _import_main("4")

    def test_importing_main_sets_the_limit(self):
        self.assertEqual(self.unset["env"], "1")

    def test_a_value_from_the_environment_does_not_bring_the_workers_back(self):
        self.assertEqual(self.preset["env"], "1")

    def test_bundled_openblas_runs_one_thread(self):
        if self.unset["openblas_threads"] is None:
            self.skipTest("numpy here does not bundle OpenBLAS")
        self.assertEqual(self.unset["openblas_threads"], 1)
        self.assertEqual(self.preset["openblas_threads"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
