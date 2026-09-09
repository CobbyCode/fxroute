#!/usr/bin/env python3
"""Measurement restore-intent gate: restore only a still-valid playback intent.

Observable contracts of main._measurement_restore_intent_matches_live_state
(the gate measurement/session.py consults before restoring the
pre-measurement playback context):

- local branch: restore proceeds only when the committed track still carries
  the captured source/id/url, the player still holds the captured file, the
  player is not ended, and the playback intent generation is unchanged;
- Spotify branch: restore additionally requires live MPRIS identities that
  intersect the captured ones and a playing/paused status;
- a changed track (the user started something new during the measurement),
  an ended player, a bumped generation, or a stopped/unknown Spotify state
  must veto the restore so it can never overwrite the newer user playback.

Only the gate predicate is pinned here (pure function of playback state,
player double and a stubbed MPRIS read); the restore sequence itself stays
covered by the session-level suites.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main

LOCAL_TRACK = {"source": "local", "id": "t1", "url": "/music/t1.flac"}
LOCAL_FILE = "/music/t1.flac"
GENERATION = 7
SPOTIFY_IDENTITIES = {"spotify:track:ABC"}


class RestoreIntentGateTests(unittest.IsolatedAsyncioTestCase):
    def _patch_live_state(
        self, *, track: dict, player_state: dict, generation: int = GENERATION
    ):
        return (
            patch.object(main.playback_state, "current_track_info", dict(track)),
            patch.object(
                main.runtime,
                "player_instance",
                SimpleNamespace(state=dict(player_state)),
            ),
            patch.object(
                main.playback_state, "playback_intent_generation", generation
            ),
        )

    async def _local_matches(
        self, *, track: dict, player_state: dict, generation: int = GENERATION
    ) -> bool:
        track_patch, player_patch, generation_patch = self._patch_live_state(
            track=track, player_state=player_state, generation=generation
        )
        with track_patch, player_patch, generation_patch:
            return await main._measurement_restore_intent_matches_live_state(
                expected_source="local",
                expected_id="t1",
                expected_url="/music/t1.flac",
                expected_file=LOCAL_FILE,
                expected_spotify_identities=set(),
                intent_generation=GENERATION,
            )

    async def test_unchanged_local_playback_may_be_restored(self):
        self.assertTrue(
            await self._local_matches(
                track=LOCAL_TRACK, player_state={"current_file": LOCAL_FILE}
            )
        )

    async def test_new_user_playback_blocks_restore(self):
        # The user started another track while the measurement was running:
        # restoring the captured intent would hijack it.
        self.assertFalse(
            await self._local_matches(
                track={"source": "local", "id": "t2", "url": "/music/t2.flac"},
                player_state={"current_file": "/music/t2.flac"},
            )
        )

    async def test_ended_player_or_bumped_generation_blocks_restore(self):
        self.assertFalse(
            await self._local_matches(
                track=LOCAL_TRACK,
                player_state={"current_file": LOCAL_FILE, "ended": True},
            ),
            "an ended player no longer matches the captured live file",
        )
        self.assertFalse(
            await self._local_matches(
                track=LOCAL_TRACK,
                player_state={"current_file": LOCAL_FILE},
                generation=GENERATION + 1,
            ),
            "a newer playback intent invalidates the captured generation",
        )

    async def test_spotify_match_allows_restore(self):
        track_patch, player_patch, generation_patch = self._patch_live_state(
            track={"source": "spotify", "id": "spotify:track:ABC"},
            player_state={},
        )
        live_spotify = {"status": "playing", "trackId": "spotify:track:ABC"}
        with (
            track_patch,
            player_patch,
            generation_patch,
            patch.object(
                main,
                "get_spotify_ui_state",
                new=AsyncMock(return_value=live_spotify),
            ),
        ):
            self.assertTrue(
                await main._measurement_restore_intent_matches_live_state(
                    expected_source="spotify",
                    expected_id=None,
                    expected_url=None,
                    expected_file=None,
                    expected_spotify_identities=set(SPOTIFY_IDENTITIES),
                    intent_generation=GENERATION,
                )
            )

    async def test_spotify_stopped_or_unknown_track_blocks_restore(self):
        for name, live_spotify in {
            "stopped": {"status": "stopped", "trackId": "spotify:track:ABC"},
            "other-track": {"status": "playing", "trackId": "spotify:track:ZZZ"},
        }.items():
            with self.subTest(name):
                track_patch, player_patch, generation_patch = self._patch_live_state(
                    track={"source": "spotify", "id": "spotify:track:ABC"},
                    player_state={},
                )
                with (
                    track_patch,
                    player_patch,
                    generation_patch,
                    patch.object(
                        main,
                        "get_spotify_ui_state",
                        new=AsyncMock(return_value=live_spotify),
                    ),
                ):
                    self.assertFalse(
                        await main._measurement_restore_intent_matches_live_state(
                            expected_source="spotify",
                            expected_id=None,
                            expected_url=None,
                            expected_file=None,
                            expected_spotify_identities=set(SPOTIFY_IDENTITIES),
                            intent_generation=GENERATION,
                        )
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
