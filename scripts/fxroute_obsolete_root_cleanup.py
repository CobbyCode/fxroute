#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Remove obsolete pre-package root modules from FXRoute installs.

The package migration (commits 00a4d86, bad63b3, ff6907d) moved every
root-level Python module into a subsystem package.  Overlay installs
(install.sh tar sync, manual rsync) leave the old root modules in place
beside the new packages.  This helper removes exactly those known-obsolete
root files, idempotently (a missing file is fine), and nothing else.

Safety rules:
- Only the files in OBSOLETE_ROOT_MODULES are ever removed.
- Removal happens only when the new package layout is present
  (NEW_LAYOUT_MARKERS), so the cleanup never runs against an install that
  has not yet received the packaged tree.
- .env, .venv, media/cache, presets, measurements, runtime state and all
  other unmanaged files are never touched.

The manifest was derived from the rename history of the migration commits
via ``git log --diff-filter=R -M --name-status`` (sources without a '/');
do not extend it by hand without a matching package rename.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Authoritative manifest: root modules renamed into subsystem packages by
# the package migration.  Sources of the rename pairs; the destinations are
# the corresponding files under measurement/, playback/, dsp/, library/,
# radio/ and audio/.
OBSOLETE_ROOT_MODULES = frozenset({
    # audio/ (commit ff6907d)
    "bluez_audio_agent.py",
    "samplerate.py",
    "samplerate_orchestration.py",
    "sink_inputs.py",
    "system_volume.py",
    "volume_contract.py",
    # audio/ (post-migration move: power.py shipped at root when the
    # system power feature was introduced, later moved under audio/)
    "power.py",
    # dsp/ (commit ff6907d)
    "dsp_api.py",
    "effects_extras.py",
    "dsp_manager.py",
    "dsp_orchestration.py",
    "peak_monitor.py",
    "dsp_persistence.py",
    "dsp_runtime.py",
    # library/ (commit ff6907d)
    "library_api.py",
    "library.py",
    "library_metadata.py",
    "playlist_io.py",
    "playlists.py",
    "music_libraries.py",
    # measurement/ (commits 00a4d86, bad63b3, ff6907d)
    "autosub.py",
    "hybrid_measurement.py",
    "measurement_analyzer.py",
    "measurement_audio.py",
    "measurement_capture_policy.py",
    "measurement_constants.py",
    "measurement_file_store.py",
    "measurement_host_capture.py",
    "measurement_job_runner.py",
    "measurement_persistence.py",
    "measurement.py",
    "measurement_repeat_runner.py",
    "measurement_routing.py",
    "measurement_session.py",
    "measurement_signal.py",
    "spl_calibration.py",
    # playback/ (commit ff6907d)
    "playback_orchestration.py",
    "playback_queue.py",
    "playback_runtime.py",
    "playback_state.py",
    "playback_transition.py",
    "player.py",
    "spotify.py",
    # radio/ (commit ff6907d)
    "radio_api.py",
    "radio_metadata.py",
    "stations.py",
})

# New-layout marker files: the cleanup only runs once every marker exists,
# i.e. after the packaged tree has been installed successfully.
NEW_LAYOUT_MARKERS = (
    "playback/player.py",
    "dsp/runtime.py",
    "library/core.py",
    "radio/stations.py",
    "audio/samplerate/__init__.py",
    "measurement/store.py",
)


def cleanup_obsolete_root_modules(root: Path) -> list[str]:
    """Remove obsolete root modules from ``root``; return removed names.

    Missing files are skipped silently (idempotent).  If the new package
    layout markers are not all present, nothing is removed.
    """
    removed: list[str] = []
    for marker in NEW_LAYOUT_MARKERS:
        if not (root / marker).is_file():
            print(
                f"[fxroute-migration] new package layout not fully installed "
                f"(missing {marker}); obsolete root cleanup skipped",
                file=sys.stderr,
            )
            return removed
    for name in sorted(OBSOLETE_ROOT_MODULES):
        target = root / name
        if not target.exists():
            continue
        if target.is_dir() or target.is_symlink():
            print(
                f"[fxroute-migration] skipping non-regular obsolete path {name}",
                file=sys.stderr,
            )
            continue
        target.unlink()
        removed.append(name)
        print(f"[fxroute-migration] removed obsolete root module {name}")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove obsolete pre-package root modules from an FXRoute install."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="FXRoute install root (default: repository root)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    removed = cleanup_obsolete_root_modules(root)
    print(f"[fxroute-migration] removed {len(removed)} obsolete root module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
