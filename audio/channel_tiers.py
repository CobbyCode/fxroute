# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded hardware reprobe for a user-selected channel tier.

The playback coordinator owns source silence, DSP lifetime and the output
gate. This module owns the WirePlumber rule, card profile and sink identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

from audio.device_profiles import cumulative_rates, profile_id
from audio.samplerate.parsing import _parse_pactl_card_active_profile, _run_command
from audio.samplerate.persistence import (
    _audio_source_selection_path,
    _load_audio_output_selection,
    _save_audio_output_selection,
)

logger = logging.getLogger(__name__)

TIER_REOPEN_TIMEOUT_SECONDS = 25.0
TIER_REOPEN_POLL_SECONDS = 0.5


def default_output_profile(cards_output: str, card_name: str) -> str | None:
    """Return the card's stock multichannel output profile, if advertised."""
    current: str | None = None
    in_profiles = False
    candidates: list[str] = []
    for raw_line in (cards_output or "").splitlines():
        line = raw_line.strip()
        if line.startswith("Name:"):
            current = line[len("Name:"):].strip()
            in_profiles = False
            continue
        if current is not None and current != card_name:
            continue
        if line == "Profiles:":
            in_profiles = True
            continue
        if in_profiles and (line.startswith("Active Profile:") or line.startswith("Ports:")):
            break
        if in_profiles and "multichannel-output" in line and "available: yes" in line:
            match = re.match(r"^(\S+):\s", line)
            if match:
                candidates.append(match.group(1))
    for candidate in candidates:
        if "+input:" in candidate:
            return candidate
    return candidates[0] if candidates else None


def prepare_change(output_key: str, tier: dict, target_rate: int, tiers: list) -> "ChannelTierChange":
    """Build an uncaptured tier change, flagging the stock-profile return.

    The largest inventory is the device's stock multichannel profile (no rule
    file); smaller ones go through the pro-audio rule.
    """
    bands = [item for item in tiers if isinstance(item, dict)]
    largest = max([int(item.get("channels") or 0) for item in bands] or [0])
    return ChannelTierChange(
        output_key, dict(tier), target_rate,
        default_tier=bool(bands) and int(tier.get("channels") or 0) >= largest,
        tiers=list(bands),
    )


class ChannelTierChange:
    def __init__(self, output_key: str, tier: dict, target_rate: int, *, run: Callable = _run_command,
                 default_tier: bool = False, tiers: list | None = None):
        if not profile_id(output_key) or not output_key.startswith("alsa_output."):
            raise ValueError("Selected output has no supported channel-tier profile")
        allowed = cumulative_rates(tiers, tier.get("id")) if tiers else list(tier.get("rates", []))
        if target_rate not in allowed:
            raise ValueError("Target rate is not available in the selected channel tier")
        self.old_key = output_key
        self.default_tier = bool(default_tier)
        prefix = output_key.rsplit(".", 1)[0]
        if self.default_tier:
            self.new_key = prefix + ".multichannel-output" if output_key.endswith(".pro-output-0") else output_key
        else:
            self.new_key = prefix + ".pro-output-0"
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
        self.cards_text = self.run(["pactl", "list", "cards"])
        self.old_profile = _parse_pactl_card_active_profile(self.cards_text, self.card)
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
        logger.info("Channel-tier reprobe start: key=%s profile=%s channels=%s rate=%s",
                    key, profile, channels, self.target_rate)
        self.run(["systemctl", "--user", "restart", "wireplumber.service"])
        deadline = time.monotonic() + TIER_REOPEN_TIMEOUT_SECONDS
        last_profile_set = 0.0
        iterations = 0
        while time.monotonic() < deadline:
            iterations += 1
            try:
                # The profile switch can race the WirePlumber restart (the
                # card is briefly gone); retry it while the sink is absent.
                if time.monotonic() - last_profile_set >= 3.0:
                    self.run(["pactl", "set-card-profile", self.card, profile])
                    last_profile_set = time.monotonic()
                sink = next((item for item in self._sinks() if item.get("name") == key), None)
                if sink is not None:
                    logger.info("Channel-tier sink present after %s polls: key=%s spec=%s",
                                iterations, key, sink.get("sample_specification") or sink.get("sample_spec"))
                    # A just-created node still settles (mute/volume writes and
                    # reads can race its appearance): settle, then confirm the
                    # gate with bounded retries instead of failing the whole
                    # reprobe on one transient readback.
                    time.sleep(0.5)
                    last_error: Exception | None = None
                    for attempt in range(3):
                        try:
                            self.run(["pactl", "set-sink-mute", key, "1"])
                            self.run(["pactl", "set-sink-volume", key, f"{self.volume}%"])
                            if self._spec(sink)[0] != channels:
                                raise ValueError(f"Channel-tier readback mismatch: expected {channels}, got {self._spec(sink)[0]}")
                            confirmed = next(item for item in self._sinks() if item.get("name") == key)
                            if not confirmed.get("mute") or abs(self._volume(confirmed) - self.volume) > 1:
                                raise ValueError("Recreated sink did not retain the output gate and volume")
                            break
                        except (RuntimeError, ValueError, KeyError) as exc:
                            last_error = exc
                            logger.info("Channel-tier gate confirm attempt %s waiting: %s", attempt + 1, exc)
                            time.sleep(0.5)
                    else:
                        raise last_error or RuntimeError("Recreated sink did not retain the output gate and volume")
                    self.run(["pactl", "set-default-sink", key])
                    _save_audio_output_selection(key)
                    logger.info("Channel-tier reprobe done: key=%s polls=%s", key, iterations)
                    return
            except RuntimeError as exc:
                logger.info("Channel-tier reprobe poll %s waiting: %s", iterations, exc)
            time.sleep(TIER_REOPEN_POLL_SECONDS)
        raise RuntimeError(
            f"Hardware sink {key} did not return within {TIER_REOPEN_TIMEOUT_SECONDS:.0f}s "
            f"after channel-tier reprobe (polls={iterations})"
        )

    def apply(self) -> None:
        if not self.captured:
            raise RuntimeError("Channel-tier change has no hardware snapshot")
        self.started = True
        self._release_and_pin(self.target_rate)
        if self.default_tier:
            profile = default_output_profile(self.cards_text, self.card)
            if profile is None:
                raise RuntimeError("Selected hardware card has no multichannel output profile")
            self._write_rule(None)
            self._reopen(self.new_key, profile, self.tier["channels"])
            old_suffix, new_suffix = ".pro-input-0", ".multichannel-input"
        else:
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
            old_suffix, new_suffix = ".multichannel-input", ".pro-input-0"
        if self.old_source_selection:
            old_source = self.old_key.replace("alsa_output.", "alsa_input.", 1).rsplit(".", 1)[0] + old_suffix
            new_source = self.new_key.replace("alsa_output.", "alsa_input.", 1).rsplit(".", 1)[0] + new_suffix
            _audio_source_selection_path().write_bytes(self.old_source_selection.replace(old_source.encode(), new_source.encode()))

    def rollback(self) -> None:
        if not self.started:
            return
        self._release_and_pin(self.old_rate)
        self._write_rule(self.old_rule)
        self._reopen(self.old_key, self.old_profile, self.old_channels)
        if self.old_source_selection is not None:
            _audio_source_selection_path().write_bytes(self.old_source_selection)
