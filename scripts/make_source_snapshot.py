#!/usr/bin/env python3
"""Create a source snapshot ZIP of the FXRoute working tree.

Includes all real source code, static/media assets, and uncommitted
working-tree changes. Excludes VCS metadata, machine-local data, build
artifacts, and reproducible caches.
"""

import argparse
import datetime
import fnmatch
import os
import subprocess
import sys
import zipfile

EXCLUDE_PATTERNS = [
    # VCS metadata
    ".git",
    # Local-only demo library and machine-local data
    "demo-library",
    ".ua",
    "AGENTS.md",
    # Python bytecode
    "__pycache__",
    "*.pyc",
    "*.pyo",
    # Virtualenvs, deps, local scratch
    ".venv",
    "venv",
    "node_modules",
    "test-inputs",
    # Secrets / local env
    ".env",
    ".env.local",
    # Runtime / temp
    "tmp",
    "*.sock",
    "*.log",
    # Editor / OS
    ".vscode",
    ".idea",
    ".DS_Store",
    # Backups, archives, and other generated artifacts
    "backups",
    "manual_backup.zip",
    "*.zip",
    "*.tar.gz",
    "media/cache",
    "media/reference",
    # Native build artifacts
    "native_dsp/build",
]


def excluded(rel_path):
    base = os.path.basename(rel_path.rstrip("/"))
    for pattern in EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(base, pattern):
            return True
    return False


def version_stamp():
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "dev"


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_out = "fxroute-src-{}-{}.zip".format(
        version_stamp(), datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=default_out,
                        help="output ZIP path (default: %(default)s)")
    args = parser.parse_args()

    out_path = args.output if os.path.isabs(args.output) else \
        os.path.join(repo_root, args.output)
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    count = 0
    total_uncompressed = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(repo_root):
            if dirpath == repo_root:
                pass
            else:
                if excluded(os.path.relpath(dirpath, repo_root)):
                    dirnames[:] = []
                    continue
            dirnames[:] = [d for d in dirnames
                           if not excluded(os.path.relpath(dirpath, repo_root) + "/" + d)]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, repo_root)
                if excluded(rel):
                    continue
                zf.write(full, rel)
                count += 1
                total_uncompressed += os.path.getsize(full)

    compressed = os.path.getsize(out_path)
    print("Wrote {}: {} files, {} -> {} ({:.0f}% compression)".format(
        out_path, count,
        _human(total_uncompressed), _human(compressed),
        100.0 * (1.0 - compressed / total_uncompressed)
        if total_uncompressed else 0.0))
    return 0


def _human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024
    return "{} B".format(size)


if __name__ == "__main__":
    sys.exit(main())
