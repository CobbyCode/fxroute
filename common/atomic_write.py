# SPDX-License-Identifier: AGPL-3.0-only

"""Canonical atomic text-file replace used by every JSON store.

Single owner of the write-to-temp-then-rename contract previously
duplicated in dsp/persistence.py, library/playlists.py and
radio/stations.py (identical observable behavior, verified by
scripts/test_atomic_write.py): the text is written to a temp file in the
same directory, flushed, fsynced and closed, then atomically renamed over
the target. Readers observe either the old or the new complete content,
never a truncated or partial file.

An existing regular target keeps its permission mode (fchmod, no symlink
following); a new target keeps the safe mkstemp default (0600). The
process umask is never modified. The descriptor is closed on every error
path and the temp file is removed best-effort. Both ``str`` and ``Path``
targets are accepted.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str) -> None:
    """Replace ``path`` with ``text`` without exposing partial content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            try:
                existing_mode = os.lstat(path).st_mode
            except OSError:
                existing_mode = None
            if existing_mode is not None and stat.S_ISREG(existing_mode):
                os.fchmod(fd, stat.S_IMODE(existing_mode))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
