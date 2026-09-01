# SPDX-License-Identifier: AGPL-3.0-only

"""Startup self-recovery for a degraded WirePlumber card probe.

After a degraded USB/boot state WirePlumber can enumerate a saved ALSA card
without its output profile, so the persisted hardware sink is missing while
the card itself is present.  A single WirePlumber restart re-probes the card
and re-applies the saved profile (verified on the FXRoute test machine), so
startup runs this bounded recovery before re-applying the persisted output
selection.  The recovery never runs when the card is absent (device really
unplugged) or when the saved selection is not a local ALSA sink.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .parsing import _parse_pactl_sinks_short, _run_command
from .persistence import _load_audio_output_selection

logger = logging.getLogger(__name__)

RECOVERY_WAIT_SECONDS = 20.0
RECOVERY_POLL_SECONDS = 1.0


def _alsa_card_name_for_sink_key(key: str) -> str | None:
    """Return the ALSA card name behind a local ``alsa_output.*`` sink key."""
    if not key.startswith("alsa_output."):
        return None
    body = key[len("alsa_output."):]
    card_body, _, _profile = body.rpartition(".")
    if not card_body:
        return None
    return f"alsa_card.{card_body}"


def _sink_names() -> set[str]:
    return {
        str(sink.get("name") or "")
        for sink in _parse_pactl_sinks_short(_run_command(["pactl", "list", "sinks", "short"]))
    }


def _card_names() -> set[str]:
    names: set[str] = set()
    for line in _run_command(["pactl", "list", "cards", "short"]).splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[1].strip():
            names.add(fields[1].strip())
    return names


def recover_saved_output_sink() -> dict[str, Any]:
    """Restore a missing persisted hardware sink by re-probing WirePlumber.

    Detection: the persisted selection names a local ALSA sink that is absent
    while its ALSA card is still enumerated.  Recovery: one bounded WirePlumber
    restart followed by polling for the sink to come back.  Returns a report
    dict (``attempted``/``recovered``/``reason``) and never raises for
    decisionable states; probe/command errors propagate to the caller.
    """
    report: dict[str, Any] = {"attempted": False, "recovered": False, "reason": None}
    selected_key = (_load_audio_output_selection() or {}).get("selected_key")
    if not selected_key:
        report["reason"] = "no-saved-selection"
        return report
    card_name = _alsa_card_name_for_sink_key(str(selected_key))
    if not card_name:
        report["reason"] = "saved-selection-not-local-alsa"
        return report
    if str(selected_key) in _sink_names():
        report["reason"] = "saved-sink-present"
        return report
    if card_name not in _card_names():
        report["reason"] = "card-absent"
        return report

    report["attempted"] = True
    logger.warning(
        "Saved output sink %s is missing while card %s is present; restarting WirePlumber once to re-probe the card",
        selected_key,
        card_name,
    )
    _run_command(["systemctl", "--user", "restart", "wireplumber.service"])

    deadline = time.monotonic() + RECOVERY_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(RECOVERY_POLL_SECONDS)
        try:
            if str(selected_key) in _sink_names():
                report["recovered"] = True
                report["reason"] = "sink-restored"
                logger.info("Saved output sink %s restored after WirePlumber re-probe", selected_key)
                return report
        except Exception:
            continue
    report["reason"] = "sink-did-not-return"
    logger.warning(
        "Saved output sink %s did not return within %.0fs after WirePlumber re-probe; continuing with current fallback",
        selected_key,
        RECOVERY_WAIT_SECONDS,
    )
    return report
