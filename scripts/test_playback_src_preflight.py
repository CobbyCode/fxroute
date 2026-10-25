#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""An unloadable SRC plugin must refuse MPV playback up front, not after the gate.

Every output stream is bound to the SRC converter, so a broken
native_src/build links make all playback fail.  The runtime snapshot has to
name that cause before any state is touched; a late failure only surfaces as
"native MPV queue target did not settle while paused".
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import src_policy
from playback.runtime.snapshot import _RuntimeSnapshotMixin
from playback.transition import PlaybackPreflightError, TransitionRequest

REASON = "Common SRC runtime is missing; run native_src/build.py"


class _Deps:
    def __init__(self):
        self.reads = []

    def player_is_running(self):
        return False

    def get_samplerate_status(self):
        self.reads.append("status")
        return {"active_rate": 44100, "force_rate": 44100}

    def get_audio_output_overview(self, status=None):
        self.reads.append("overview")
        return {"output_mode": {"mode": "stereo"}}

    def get_current_track_info(self):
        return {}

    def get_playback_intent_generation(self):
        return 0


class _Adapter(_RuntimeSnapshotMixin):
    def __init__(self):
        self._deps = _Deps()
        self._player = None
        self._dsp_runtime = None
        self._staged_target_url = None


def play(source="local", **overrides):
    fields = dict(operation="play", source=source, target_url="/music/a.flac", target_rate=44100)
    fields.update(overrides)
    return TransitionRequest(**fields)


class PlaybackSrcPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_mpv_source_start_is_refused_before_any_graph_read(self):
        for source in ("local", "radio", "tidal"):
            adapter = _Adapter()
            with patch.object(src_policy, "playback_error", return_value=REASON):
                with self.assertRaisesRegex(PlaybackPreflightError, "native_src/build.py"):
                    await adapter.read_transition_snapshot(play(source))
            self.assertEqual(adapter._deps.reads, [], f"{source}: graph read before the refusal")

    async def test_requests_that_open_no_mpv_stream_are_not_checked(self):
        not_checked = {
            "pause": play(should_play=False),
            "resume without reload": play(reload_source=False),
            "graph only": play(graph_only=True),
            "spotify": play("spotify", target_url=None),
            "qobuz": play("qobuz", target_url=None),
        }
        for label, request in not_checked.items():
            adapter = _Adapter()
            with patch.object(src_policy, "playback_error", return_value=REASON) as check:
                snapshot = await adapter.read_transition_snapshot(request)
            check.assert_not_called()
            self.assertIn("player", snapshot, label)

    async def test_healthy_runtime_lets_the_snapshot_complete(self):
        adapter = _Adapter()
        with patch.object(src_policy, "playback_error", return_value=None) as check:
            snapshot = await adapter.read_transition_snapshot(play())
        check.assert_called_once_with()
        self.assertEqual(snapshot["active_rate"], 44100)

    async def test_dangling_build_links_after_a_deploy_refuse_playback(self):
        """The real incident: stable links pointing at an empty generation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "native_src/build"
            runtime.mkdir(parents=True)
            (runtime / "runtime-empty").mkdir()
            (runtime / "current").symlink_to("runtime-empty")
            (runtime / "libspa-fxroute-src.so").symlink_to("current/libspa-fxroute-src.so")
            binding = root / "pipewire/client.conf.d/80-fxroute-src.conf"
            binding.parent.mkdir(parents=True)
            binding.write_text("stream.rules = []\n")
            with patch.object(src_policy, "RUNTIME", runtime), \
                    patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root)}):
                adapter = _Adapter()
                with self.assertRaisesRegex(PlaybackPreflightError, "runtime is missing"):
                    await adapter.read_transition_snapshot(play())
                self.assertEqual(adapter._deps.reads, [])

                (runtime / "runtime-built").mkdir()
                (runtime / "runtime-built/libspa-fxroute-src.so").write_bytes(b"plugin")
                (runtime / "current").unlink()
                (runtime / "current").symlink_to("runtime-built")
                snapshot = await _Adapter().read_transition_snapshot(play())
                self.assertEqual(snapshot["active_rate"], 44100)


if __name__ == "__main__":
    unittest.main()
