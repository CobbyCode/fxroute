"""Central queue-state install/restore support for tests.

Tests exercise the authoritative ``playback.queue`` singleton instead of
patching ``main.playback_queue*`` globals (removed in Pass 2).  This helper
saves, installs and restores the seven queue state values of the one
PlaybackQueue instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.queue import PlaybackQueue, queue


def queue_state(obj: Optional[PlaybackQueue] = None) -> dict[str, Any]:
    q = obj if obj is not None else queue
    return {
        "tracks": [dict(track) for track in q.tracks],
        "original": [dict(track) for track in q.original],
        "index": q.index,
        "mode": q.mode,
        "loop": q.loop,
        "shuffle": q.shuffle,
        "single_track_loop": q.single_track_loop,
    }


def restore_queue_state(saved: dict[str, Any], obj: Optional[PlaybackQueue] = None) -> None:
    q = obj if obj is not None else queue
    q.tracks = [dict(track) for track in saved["tracks"]]
    q.original = [dict(track) for track in saved["original"]]
    q.index = saved["index"]
    q.mode = saved["mode"]
    q.loop = saved["loop"]
    q.shuffle = saved["shuffle"]
    q.single_track_loop = saved["single_track_loop"]
