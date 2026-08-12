"""Central queue-state install/restore support for tests.

Tests exercise the authoritative ``playback_queue.queue`` singleton instead of
patching ``main.playback_queue*`` globals (removed in Pass 2).  This helper
saves, installs and restores the seven queue state values of the one
PlaybackQueue instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback_queue import PlaybackQueue, queue


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


def apply_queue_state(state: dict[str, Any], obj: Optional[PlaybackQueue] = None) -> None:
    q = obj if obj is not None else queue
    q.tracks = [dict(track) for track in state.get("tracks", ())]
    original = state.get("original")
    q.original = (
        [dict(track) for track in original]
        if original is not None
        else [dict(track) for track in q.tracks]
    )
    q.index = state.get("index", -1)
    q.mode = state.get("mode", "app_replace")
    q.loop = bool(state.get("loop", False))
    q.shuffle = bool(state.get("shuffle", False))
    q.single_track_loop = bool(state.get("single_track_loop", False))


class queue_state_patch:
    """Context manager: install a queue state, restore the previous one on exit.

    Mirrors the ``patch.object(main, "playback_queue*", ...)`` pattern used
    before Pass 2 against the single authoritative queue instance.
    """

    def __init__(self, *, tracks=(), original=None, index: int = -1,
                 mode: str = "app_replace", loop: bool = False,
                 shuffle: bool = False, single_track_loop: bool = False,
                 obj: Optional[PlaybackQueue] = None) -> None:
        self.obj = obj if obj is not None else queue
        self.saved = queue_state(self.obj)
        self.state = {
            "tracks": tracks,
            "original": original,
            "index": index,
            "mode": mode,
            "loop": loop,
            "shuffle": shuffle,
            "single_track_loop": single_track_loop,
        }

    def __enter__(self) -> PlaybackQueue:
        apply_queue_state(self.state, self.obj)
        return self.obj

    def __exit__(self, *exc_info) -> None:
        restore_queue_state(self.saved, self.obj)
        return None
