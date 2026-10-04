#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Provider release requires a successful PipeWire sink-input inspection."""

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playback import media_readiness


class ProviderReleaseInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_pactl_probe_cannot_confirm_provider_release(self):
        failures = (SimpleNamespace(returncode=1, stdout="", stderr="Connection refused"),
                    OSError("pactl missing"), subprocess.TimeoutExpired("pactl", 1.5))
        for provider in ("mpv", "spotify", "qobuz"):
            for failure in failures:
                with self.subTest(provider=provider, failure=failure):
                    kwargs = {"side_effect": failure} if isinstance(failure, BaseException) else {"return_value": failure}
                    with patch("audio.sink_inputs.subprocess.run", **kwargs):
                        released = await getattr(media_readiness, f"wait_for_pipewire_{provider}_release")(timeout_ms=0)
                    self.assertFalse(released)

    async def test_successful_empty_probe_confirms_provider_release(self):
        for provider in ("mpv", "spotify", "qobuz"):
            with patch("audio.sink_inputs.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")):
                self.assertTrue(await getattr(media_readiness, f"wait_for_pipewire_{provider}_release")(timeout_ms=0))

    async def test_corked_historical_provider_streams_do_not_block_release(self):
        for provider, application in (("spotify", "spotify"), ("qobuz", "qbzd")):
            output = f'Sink Input #42\n\tCorked: yes\n\tMute: no\n\tProperties:\n\t\tapplication.name = "{application}"\n'
            with patch("audio.sink_inputs.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertTrue(await getattr(media_readiness, f"wait_for_pipewire_{provider}_release")(timeout_ms=0))


if __name__ == "__main__":
    unittest.main()
