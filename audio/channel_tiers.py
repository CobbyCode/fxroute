# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded hardware reprobe for a user-selected channel tier.

The playback coordinator owns source silence, DSP lifetime and the output
gate. This module owns the WirePlumber rule, card profile and sink identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from audio.device_profiles import profile_id
from audio.samplerate.parsing import _parse_pactl_card_active_profile, _run_command
from audio.samplerate.persistence import (
    _audio_source_selection_path,
    _load_audio_output_selection,
    _save_audio_output_selection,
)


class ChannelTierChange:
    def __init__(self, output_key: str, tier: dict, target_rate: int, *, run: Callable = _run_command):
        if not profile_id(output_key) or not output_key.startswith("alsa_output."):
            raise ValueError("Selected output has no supported channel-tier profile")
        if target_rate not in tier.get("rates", []):
            raise ValueError("Target rate is not available in the selected channel tier")
        self.old_key = output_key
        self.new_key = output_key.rsplit(".", 1)[0] + ".pro-output-0"
        self.card = output_key.replace("alsa_output.", "alsa_card.", 1).rsplit(".", 1)[0]
        self.tier, self.target_rate, self.run = dict(tier), target_rate, run
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        digest = hashlib.sha256(self.card.encode()).hexdigest()[:16]
        self.rule_path = root / "wireplumber" / "wireplumber.conf.d" / f"90-fxroute-tier-{digest}.conf"
        self.captured = False
        self.started = False

    def _sinks(self) -> list[dict]:
        return json.loads(self.run(["pactl", "-f", "json", "list", "sinks"]))

    @staticmethod
    def _spec(sink: dict) -> tuple[int, int]:
        spec = str(sink.get("sample_specification") or sink.get("sample_spec") or "")
        match = re.search(r"(\d+)ch\s+(\d+)Hz", spec)
        if not match:
            raise RuntimeError("Hardware sink sample specification is unavailable")
        return int(match[1]), int(match[2])

    @staticmethod
    def _volume(sink: dict) -> int:
        values = [float(str(item["value_percent"]).rstrip("%")) for item in sink.get("volume", {}).values()]
        if not values:
            raise RuntimeError("Hardware sink volume is unavailable")
        return round(max(values))

    def capture(self) -> None:
        if _load_audio_output_selection().get("selected_key") != self.old_key:
            raise ValueError("Selected output changed; refresh audio settings")
        sink = next((item for item in self._sinks() if item.get("name") == self.old_key), None)
        if not sink:
            raise RuntimeError("Selected hardware sink is unavailable")
        self.old_channels, self.old_rate = self._spec(sink)
        self.volume = self._volume(sink)
        self.old_profile = _parse_pactl_card_active_profile(self.run(["pactl", "list", "cards"]), self.card)
        if not self.old_profile or self.old_profile == "off":
            raise RuntimeError("Selected hardware card has no active profile")
        self.old_rule = self.rule_path.read_bytes() if self.rule_path.exists() else None
        source_path = _audio_source_selection_path()
        self.old_source_selection = source_path.read_bytes() if source_path.exists() else None
        self.captured = True

    def _write_rule(self, content: bytes | None) -> None:
        if content is None:
            self.rule_path.unlink(missing_ok=True)
            return
        self.rule_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.rule_path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(self.rule_path)

    def _release_and_pin(self, rate: int) -> None:
        self.run(["pactl", "set-card-profile", self.card, "off"])
        self.run(["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate)])

    def _reopen(self, key: str, profile: str, channels: int) -> None:
        self.run(["systemctl", "--user", "restart", "wireplumber.service"])
        deadline = time.monotonic() + 15.0
        profile_set = False
        while time.monotonic() < deadline:
            try:
                if not profile_set:
                    self.run(["pactl", "set-card-profile", self.card, profile])
                    profile_set = True
                sink = next((item for item in self._sinks() if item.get("name") == key), None)
                if sink is not None:
                    self.run(["pactl", "set-sink-mute", key, "1"])
                    self.run(["pactl", "set-sink-volume", key, f"{self.volume}%"])
                    if self._spec(sink)[0] != channels:
                        raise ValueError(f"Channel-tier readback mismatch: expected {channels}, got {self._spec(sink)[0]}")
                    confirmed = next(item for item in self._sinks() if item.get("name") == key)
                    if not confirmed.get("mute") or abs(self._volume(confirmed) - self.volume) > 1:
                        raise ValueError("Recreated sink did not retain the output gate and volume")
                    self.run(["pactl", "set-default-sink", key])
                    _save_audio_output_selection(key)
                    return
            except RuntimeError:
                pass
            time.sleep(0.15)
        raise RuntimeError("Hardware sink did not return after channel-tier reprobe")

    def apply(self) -> None:
        if not self.captured:
            raise RuntimeError("Channel-tier change has no hardware snapshot")
        self.started = True
        self._release_and_pin(self.target_rate)
        rule = {"monitor.alsa.rules": [{
            "matches": [{"device.name": self.card}],
            "actions": {"update-props": {
                "api.acp.pro-channels": self.tier["channels"],
                "api.acp.probe-rate": self.tier["probe_rate"],
                "device.profile": "pro-audio",
            }},
        }]}
        self._write_rule((json.dumps(rule, indent=2) + "\n").encode())
        self._reopen(self.new_key, "pro-audio", self.tier["channels"])
        if self.old_source_selection:
            old_source = self.old_key.replace("alsa_output.", "alsa_input.", 1).rsplit(".", 1)[0] + ".multichannel-input"
            new_source = old_source.rsplit(".", 1)[0] + ".pro-input-0"
            _audio_source_selection_path().write_bytes(self.old_source_selection.replace(old_source.encode(), new_source.encode()))

    def rollback(self) -> None:
        if not self.started:
            return
        self._release_and_pin(self.old_rate)
        self._write_rule(self.old_rule)
        self._reopen(self.old_key, self.old_profile, self.old_channels)
        if self.old_source_selection is not None:
            _audio_source_selection_path().write_bytes(self.old_source_selection)
