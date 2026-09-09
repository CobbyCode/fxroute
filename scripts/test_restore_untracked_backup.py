#!/usr/bin/env python3
"""Restore must back up untracked user files instead of deleting them.

Contract for restore_main in scripts/update_fxroute.sh (no production code
changed here):

- a dirty tree (tracked modification + untracked user files) restores to
  origin/main: the tracked diff lands in backups/*.patch (existing
  behavior, preserved);
- untracked user files outside the documented runtime excludes
  (media/cache, .env, .env.local, .venv, backups, BUILD_ID) are archived
  to backups/local-untracked-*.tar.gz with intact content before
  `git clean -fd` removes them from the tree;
- excluded runtime files (.env, media/cache) survive in place;
- the harness stays alive with return code 0.

The pre-fix path saved only the tracked diff and let `git clean -fd`
delete every other untracked file unsaved, while update mode (main)
refuses to run on any dirty tree at all. The test uses a real local git
repo (clone of a bare origin, no network); only reconcile_checkout is
stubbed out (deployment side effects).
"""

import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_fxroute.sh"
SCRIPT_TEXT = SCRIPT.read_text()


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


HELPERS = "\n".join(
    extract_function(SCRIPT_TEXT, name)
    for name in (
        "setup_repo",
        "version_at",
        "git_short",
        "git_remote_ref",
        "restore_main",
    )
)


def make_repo(root: Path) -> Path:
    """Create origin bare repo + work clone with main.py/requirements/VERSION."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = root / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "main.py").write_text("print('fxroute')\n")
    (work / "requirements.txt").write_text("fastapi\n")
    (work / "VERSION").write_text("9.9.9\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "base"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"], check=True
    )
    # Production checkouts track origin/main (verified on the appliance);
    # configure the same so upstream resolution behaves identically.
    subprocess.run(
        ["git", "-C", str(work), "branch", "--set-upstream-to=origin/main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "set-head", "origin", "main"], check=True
    )
    return work

def run_restore(work: Path) -> subprocess.CompletedProcess:
    harness = f"""
set -Eeuo pipefail
log() {{ printf '[fxroute-update] %s\\n' "$*"; }}
die() {{ printf '[fxroute-update][error] %s\\n' "$*" >&2; exit 1; }}
{HELPERS}
resolve_repo_path() {{ printf '%s\\n' "$FXROUTE_REPO_PATH"; }}
reconcile_checkout() {{ printf 'reconcile-stubbed\\n'; }}
export FXROUTE_REPO_PATH="{work}"
SERVICE_NAME="${{FXROUTE_SERVICE_NAME:-fxroute-test}}"
export FXROUTE_SERVICE_NAME
( restore_main )
printf 'harness-rc=%s\\n' "$?"
printf 'harness-alive\\n'
"""
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


class RestoreUntrackedBackupTests(unittest.TestCase):
    def test_dirty_tree_restore_preserves_untracked_user_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work = make_repo(root)
            # Tracked modification.
            with open(work / "main.py", "a") as handle:
                handle.write("print('local hack')\n")
            # Untracked user files (must be archived, then cleaned).
            (work / "my-notes.txt").write_text("mix settings 42\n")
            (work / "recordings").mkdir()
            (work / "recordings" / "take1.txt").write_text("take one\n")
            # Documented runtime excludes (must survive in place).
            (work / ".env").write_text("SECRET=x\n")
            (work / "media" / "cache").mkdir(parents=True)
            (work / "media" / "cache" / "cover.bin").write_bytes(b"\x00" * 8)

            result = run_restore(work)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("harness-alive", result.stdout)
            # Tracked diff backup (existing behavior, preserved).
            patches = sorted((work / "backups").glob("local-changes-*.patch"))
            self.assertEqual(len(patches), 1, "tracked diff must be saved as patch")
            self.assertIn("local hack", patches[0].read_text())
            # Untracked user files archived with intact content...
            archives = sorted((work / "backups").glob("local-untracked-*.tar.gz"))
            self.assertEqual(
                len(archives),
                1,
                "untracked user files must be archived, not silently deleted",
            )
            with tarfile.open(archives[0], "r:gz") as archive:
                names = archive.getnames()
                self.assertIn("my-notes.txt", names)
                self.assertIn("recordings/take1.txt", names)
                self.assertEqual(
                    archive.extractfile("my-notes.txt").read(), b"mix settings 42\n"
                )
                self.assertEqual(
                    archive.extractfile("recordings/take1.txt").read(), b"take one\n"
                )
            # ...and removed from the tree by the clean (fresh release state).
            self.assertFalse((work / "my-notes.txt").exists())
            self.assertFalse((work / "recordings").exists())
            # Excluded runtime files survive in place.
            self.assertEqual((work / ".env").read_text(), "SECRET=x\n")
            self.assertTrue((work / "media" / "cache" / "cover.bin").exists())
            # Tracked modification is gone (reset worked).
            self.assertNotIn(
                "local hack", (work / "main.py").read_text()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
