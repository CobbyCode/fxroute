#!/usr/bin/env python3
"""Publish the FXRoute web demo to GitHub Pages (project site /fxroute/).

The snapshot is staged ONLY in a dedicated gh-pages worktree outside the
main checkout (default: <checkout>/../fxroute-pages). This script never
deletes anything in the main checkout: it refuses any worktree path that
resolves inside the main checkout directory.

Usage:
    python3 scripts/publish_pages.py [--worktree PATH] [--base-path /fxroute/] [--push]

Without --push, the worktree is updated and committed but nothing is
uploaded. With --push, the gh-pages branch is pushed to origin.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKTREE = os.path.join(os.path.dirname(ROOT), "fxroute-pages")
SNAPSHOT_FILES = ("index.html", "demo", "static")


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worktree", default=DEFAULT_WORKTREE)
    parser.add_argument("--base-path", default="/fxroute/")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    worktree = os.path.realpath(args.worktree)
    main_checkout = os.path.realpath(ROOT)
    if worktree == main_checkout or worktree.startswith(main_checkout + os.sep):
        sys.exit("refusing: worktree must be outside the main checkout")

    out = os.path.join(
        tempfile.mkdtemp(prefix="fxroute-publish-"), "publish"
    )
    run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "build_demo.py"),
            "--base-path",
            args.base_path,
            "--out",
            out,
        ],
        ROOT,
    )

    for name in SNAPSHOT_FILES:
        src = os.path.join(out, name)
        dst = os.path.join(worktree, name)
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        elif os.path.lexists(dst):
            os.remove(dst)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    shutil.rmtree(os.path.dirname(out), ignore_errors=True)

    status = run_git(["status", "--porcelain=v1"], worktree)
    if not status.strip():
        print("pages worktree already up to date")
        return
    run_git(["add", "-A", "--", "."], worktree)
    run_git(["commit", "-q", "-m", "Update demo snapshot"], worktree)
    print(run_git(["log", "--oneline", "-1"], worktree))
    if args.push:
        run_git(["push", "origin", "gh-pages"], worktree)
    else:
        print("committed locally; rerun with --push to upload")


def run_git(cmd, cwd):
    return subprocess.run(
        ["git"] + cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


if __name__ == "__main__":
    main()
