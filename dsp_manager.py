# SPDX-License-Identifier: AGPL-3.0-only
"""FXRoute-owned DSP preset, state, and native-engine configuration manager."""

import copy
import json
import logging
import math
import re
import shutil
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dsp_persistence import DSPPresetStore, DSPStateStore, clean_name
from system_volume import volume_db_to_percent, volume_percent_to_db

logger = logging.getLogger(__name__)


class UnsupportedPluginError(ValueError):
    pass


class DSPManager:
    """EasyEffects-call-site compatible manager with no EasyEffects runtime dependency."""

    PURE_PRESET = "Direct"
    PROTECTED_PRESETS = {"Direct", "Neutral"}
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
    PRESET_SCHEMA = DSPPresetStore.SCHEMA
    PRESET_VERSION = DSPPresetStore.VERSION
    ENGINE_SCHEMA = "fxroute.dsp.engine"
    ENGINE_VERSION = 1
    SUPPORTED_PLUGINS = {
        "equalizer", "convolver", "delay", "limiter", "headroom",
        "bass_enhancer", "autogain", "loudness", "crystalizer", "maximizer",
    }
    LIMITER_DEFAULTS = {"enabled": True, "params": {"thresholdDb": -1.0, "attackMs": 5.0, "releaseMs": 50.0, "lookaheadMs": 5.0, "stereoLinkPercent": 100.0}}
    LIMITER_THRESHOLD_MIN_DB = -24.0
    LIMITER_THRESHOLD_MAX_DB = 0.0
    LIMITER_ATTACK_MIN_MS = 0.1
    LIMITER_ATTACK_MAX_MS = 100.0
    LIMITER_RELEASE_MIN_MS = 1.0
    LIMITER_RELEASE_MAX_MS = 1000.0
    LIMITER_LOOKAHEAD_MAX_MS = 20.0
    HEADROOM_DEFAULTS = {"enabled": False, "params": {"gainDb": -3.0}}
    DELAY_DEFAULTS = {"enabled": False, "params": {"leftMs": 0.0, "rightMs": 0.0}}
    DELAY_MAX_MS = 500.0
    BASS_ENHANCER_DEFAULTS = {"enabled": False, "params": {"amount": 0.0, "harmonics": 8.5, "scope": 100.0, "blend": 0.0}}
    BASS_AMOUNT_MIN_DB = -20.0
    BASS_AMOUNT_MAX_DB = 20.0
    BASS_HARMONICS_MIN = 1.0
    BASS_HARMONICS_MAX = 20.0
    BASS_SCOPE_MIN_HZ = 20.0
    BASS_SCOPE_MAX_HZ = 500.0
    BASS_BLEND_MIN_PERCENT = -100.0
    BASS_BLEND_MAX_PERCENT = 100.0
    AUTOGAIN_DEFAULTS = {"enabled": False, "params": {"targetDb": -12.0, "reference": "Geometric Mean (MSI)", "silenceThresholdDb": -70.0, "maximumHistorySeconds": 15}}
    AUTOGAIN_REFERENCES = ("Momentary", "Shortterm", "Integrated",
                           "Geometric Mean (MSI)", "Geometric Mean (MS)",
                           "Geometric Mean (MI)", "Geometric Mean (SI)")
    AUTOGAIN_SILENCE_THRESHOLD_MIN_DB = -100.0
    AUTOGAIN_SILENCE_THRESHOLD_MAX_DB = 0.0
    AUTOGAIN_HISTORY_MIN_SECONDS = 6
    AUTOGAIN_HISTORY_MAX_SECONDS = 3600
    LOUDNESS_DEFAULTS = {"enabled": False, "params": {"fftSize": 4096, "strength": 10, "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}}}
    LOUDNESS_PLUGIN_VOLUME_MIN_DB = -83.0
    LOUDNESS_PLUGIN_VOLUME_MAX_DB = 7.0
    LOUDNESS_STRENGTH_GUARD_DB = 18.0
    LOUDNESS_OUTPUT_GAIN_MIN_DB = -36.0
    LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0.35
    TONE_EFFECT_DEFAULTS = {"enabled": False, "mode": "crystalizer"}

    loudness_db_from_percent = staticmethod(volume_percent_to_db)
    loudness_percent_from_db = staticmethod(volume_db_to_percent)

    def normalize_effects_extras(self, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        source = extras if isinstance(extras, dict) else {}
        result = copy.deepcopy({
            "limiter": self.LIMITER_DEFAULTS, "headroom": self.HEADROOM_DEFAULTS,
            "delay": self.DELAY_DEFAULTS, "bass_enhancer": self.BASS_ENHANCER_DEFAULTS,
            "autogain": self.AUTOGAIN_DEFAULTS, "loudness": self.LOUDNESS_DEFAULTS,
            "tone_effect": self.TONE_EFFECT_DEFAULTS,
        })
        for key in result:
            value = source.get(key)
            if isinstance(value, str) and key == "tone_effect":
                result[key] = {"mode": value}
                continue
            if isinstance(value, dict):
                result[key].update({name: copy.deepcopy(item) for name, item in value.items() if name != "params"})
                if isinstance(value.get("params"), dict):
                    result[key].setdefault("params", {}).update(copy.deepcopy(value["params"]))
        headroom = float(result["headroom"]["params"]["gainDb"])
        if not headroom.is_integer() or not -9 <= headroom <= 0:
            raise ValueError("headroom.params.gainDb must be a whole dB value between -9 and 0")
        tone = result["tone_effect"]
        raw_mode = str(tone.get("mode", self.TONE_EFFECT_DEFAULTS["mode"]) or self.TONE_EFFECT_DEFAULTS["mode"]).strip().lower()
        enabled = bool(tone.get("enabled", raw_mode != "off"))
        if raw_mode == "off":
            mode = self.TONE_EFFECT_DEFAULTS["mode"]
            enabled = False
        else:
            mode = raw_mode
        if mode not in {"crystalizer", "maximizer"}:
            raise ValueError("tone_effect.mode must be one of: crystalizer, maximizer")
        result["tone_effect"] = {"enabled": enabled, "mode": mode}
        limiter = result["limiter"]["params"]
        threshold_db = float(limiter["thresholdDb"])
        if not self.LIMITER_THRESHOLD_MIN_DB <= threshold_db <= self.LIMITER_THRESHOLD_MAX_DB:
            raise ValueError("limiter.params.thresholdDb must be between -24 and 0")
        attack_ms = float(limiter["attackMs"])
        if not self.LIMITER_ATTACK_MIN_MS <= attack_ms <= self.LIMITER_ATTACK_MAX_MS:
            raise ValueError("limiter.params.attackMs must be between 0.1 and 100")
        release_ms = float(limiter["releaseMs"])
        if not self.LIMITER_RELEASE_MIN_MS <= release_ms <= self.LIMITER_RELEASE_MAX_MS:
            raise ValueError("limiter.params.releaseMs must be between 1 and 1000")
        lookahead_ms = float(limiter["lookaheadMs"])
        if not 0.0 <= lookahead_ms <= self.LIMITER_LOOKAHEAD_MAX_MS:
            raise ValueError("limiter.params.lookaheadMs must be between 0 and 20")
        stereo_link = float(limiter["stereoLinkPercent"])
        if not 0.0 <= stereo_link <= 100.0:
            raise ValueError("limiter.params.stereoLinkPercent must be between 0 and 100")
        bass = result["bass_enhancer"]["params"]
        amount = float(bass["amount"])
        if not self.BASS_AMOUNT_MIN_DB <= amount <= self.BASS_AMOUNT_MAX_DB:
            raise ValueError("bass_enhancer.params.amount must be between -20 and 20")
        harmonics = float(bass["harmonics"])
        if not self.BASS_HARMONICS_MIN <= harmonics <= self.BASS_HARMONICS_MAX:
            raise ValueError("bass_enhancer.params.harmonics must be between 1 and 20")
        scope = float(bass["scope"])
        if not self.BASS_SCOPE_MIN_HZ <= scope <= self.BASS_SCOPE_MAX_HZ:
            raise ValueError("bass_enhancer.params.scope must be between 20 and 500")
        blend = float(bass["blend"])
        if not self.BASS_BLEND_MIN_PERCENT <= blend <= self.BASS_BLEND_MAX_PERCENT:
            raise ValueError("bass_enhancer.params.blend must be between -100 and 100")
        autogain = result["autogain"]["params"]
        target = float(autogain["targetDb"])
        if target not in {-12.0, -15.0, -18.0, -23.0}:
            raise ValueError("autogain.params.targetDb must be one of -12, -15, -18, or -23")
        reference = str(autogain.get("reference") or self.AUTOGAIN_DEFAULTS["params"]["reference"]).strip() or self.AUTOGAIN_DEFAULTS["params"]["reference"]
        if reference not in self.AUTOGAIN_REFERENCES:
            raise ValueError(f"autogain.params.reference is not supported: {reference}")
        silence_threshold = float(autogain["silenceThresholdDb"])
        if not self.AUTOGAIN_SILENCE_THRESHOLD_MIN_DB <= silence_threshold <= self.AUTOGAIN_SILENCE_THRESHOLD_MAX_DB:
            raise ValueError("autogain.params.silenceThresholdDb must be between -100 and 0")
        history = float(autogain["maximumHistorySeconds"])
        if not history.is_integer():
            raise ValueError("autogain.params.maximumHistorySeconds must be a whole number of seconds")
        history = int(history)
        if not self.AUTOGAIN_HISTORY_MIN_SECONDS <= history <= self.AUTOGAIN_HISTORY_MAX_SECONDS:
            raise ValueError("autogain.params.maximumHistorySeconds must be between 6 and 3600")
        autogain["reference"] = reference
        autogain["silenceThresholdDb"] = silence_threshold
        autogain["maximumHistorySeconds"] = history
        loudness = result["loudness"]["params"]
        if int(loudness["fftSize"]) not in {256, 512, 1024, 2048, 4096, 8192, 16384}:
            raise ValueError("loudness.params.fftSize is not supported")
        strength = {"full": 10, "med": 7, "light": 4, "min": 1}.get(str(loudness["strength"]).lower(), loudness["strength"])
        if float(strength) != int(float(strength)) or not 1 <= int(float(strength)) <= 10:
            raise ValueError("loudness.params.strength must be between 1 and 10")
        loudness["strength"] = int(float(strength))
        loudness["volumeDb"] = float(loudness["volumeDb"])
        if not -80 <= loudness["volumeDb"] <= 0:
            raise ValueError("loudness.params.volumeDb must be between -80 and 0")

        def normalize_calibration(value: Any) -> Dict[str, Any]:
            calibration = dict(value) if isinstance(value, dict) else {}
            adjustment = calibration.get("requiredAdjustmentDb")
            if isinstance(adjustment, (int, float)):
                calibration["requiredAdjustmentDb"] = float(adjustment)
                calibration["calibrated"] = abs(float(adjustment)) <= 1.0
            return calibration

        loudness["calibration"] = normalize_calibration(loudness.get("calibration"))
        raw_profiles = loudness.get("calibrationProfiles") if isinstance(loudness.get("calibrationProfiles"), dict) else {}
        loudness["calibrationProfiles"] = {
            str(profile_id): normalize_calibration(profile)
            for profile_id, profile in raw_profiles.items()
            if isinstance(profile, dict)
        }
        delay = result["delay"]["params"]
        delay["leftMs"] = float(delay["leftMs"])
        delay["rightMs"] = float(delay["rightMs"])
        if not all(0 <= delay[channel] <= self.DELAY_MAX_MS for channel in ("leftMs", "rightMs")):
            raise ValueError("delay.params leftMs/rightMs must be between 0 and 500")
        return result

    @staticmethod
    def _autogain_plugin_payload(definition: Dict[str, Any]) -> Dict[str, Any]:
        params = definition["params"]
        return {"bypass": not definition["enabled"], "target": params["targetDb"]}

    @classmethod
    def _loudness_plugin_payload(cls, definition: Dict[str, Any], autogain: Dict[str, Any]) -> Dict[str, Any]:
        """Loudness stage payload: volumeDb-dependent work point, net-zero output.

        The LSP work point keeps the established FXRoute relation
        ``volumeDb - calibration + strength + AutoGain`` so the tonal
        compensation follows the canonical listening volume exactly as before
        the native-DSP migration.  The stage applies the inverse compensation
        after the plugin (``output-gain = -volume``); the native engine
        matches the plugin's FFT/OLA latency so the trim switches on the same
        frame boundary as the work-point curve, keeping the stage
        level-neutral at the pre-master post_effect meter tap.  The canonical
        listening attenuation is applied right after the tap by the
        master_gain stage and before the protection limiter, so Peak/VU stays
        independent of listening volume while the Limiter keeps the
        pre-migration input level (volumeDb).
        """
        params = definition["params"]
        calibration = params.get("calibration")
        adjustment = calibration.get("requiredAdjustmentDb") if isinstance(calibration, dict) else 0.0
        calibration_db = float(adjustment) if isinstance(adjustment, (int, float)) else 0.0
        master_db = float(params.get("volumeDb", 0.0))
        strength_db = (10 - int(params.get("strength", 10))) * (30.0 / 9.0)
        autogain_db = (float(autogain.get("params", {}).get("targetDb", -23.0)) + 23.0
                       if autogain.get("enabled") else 0.0)
        raw_volume_db = master_db - calibration_db + strength_db + autogain_db
        plugin_volume_db = max(cls.LOUDNESS_PLUGIN_VOLUME_MIN_DB,
                               min(cls.LOUDNESS_PLUGIN_VOLUME_MAX_DB, raw_volume_db))
        return {"bypass": not definition["enabled"], "fft": str(params.get("fftSize", 4096)),
                "volume": plugin_volume_db,
                "output-gain": -plugin_volume_db if definition["enabled"] else 0.0}

    def __init__(self, home: Optional[Path] = None,
                  apply_callback: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.home = Path(home or Path.home())
        self.base_dir = self.home / ".config/fxroute/dsp"
        self.output_dir = self.base_dir / "presets"
        self.irs_dir = self.base_dir / "irs"
        self.state_dir = self.base_dir / "state"
        self.db_file = self.state_dir / "active.json"
        self.global_extras_file = self.state_dir / "extras.json"
        self.compare_state_file = self.state_dir / "compare.json"
        for directory in (self.output_dir, self.irs_dir, self.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.preset_store = DSPPresetStore(self.output_dir, self.irs_dir)
        self.state_store = DSPStateStore(self.state_dir)
        self.apply_callback = apply_callback
        self.runtime_transition_callback = None
        self.temporary_runtime_transition_callback = None
        self.legacy_extras_candidates = (
            self.home / ".var/app/com.github.wwmm.easyeffects/config/easyeffects/agent-output-extras.json",
            self.home / ".config/easyeffects/agent-output-extras.json",
        )
        self._bootstrap()
        self._migrate_legacy_extras()
        self._runtime_properties = self.state_store.read("runtime.json", {})
        if not isinstance(self._runtime_properties, dict):
            self._runtime_properties = {}
        self._seed_runtime_properties()

    def _migrate_legacy_extras(self) -> None:
        """Import EasyEffects-era global extras once, before any extras.json exists.

        Preserves SPL/Loudness calibration profiles and global AutoGain/Bass
        settings from the previous system.  Presets and convolver IRs are not
        migrated; those are recreated through the normal import path.
        """
        if self.global_extras_file.exists():
            return
        migration = self.state_store.read("migration.json", {})
        if isinstance(migration, dict) and migration.get("extras_migrated"):
            return
        for candidate in self.legacy_extras_candidates:
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                normalized = self._lenient_legacy_extras(payload)
            except Exception as exc:
                logger.warning("Legacy EasyEffects extras migration skipped (%s): %s", candidate, exc)
                continue
            self.save_global_extras(normalized)
            self.state_store.write("migration.json", {
                "schema": "fxroute.dsp.migration", "version": 1,
                "extras_migrated": True, "source": str(candidate),
            })
            logger.info("Migrated EasyEffects-era global extras (incl. SPL calibration profiles) from %s", candidate)
            return

    def _lenient_legacy_extras(self, payload: Any) -> Dict[str, Any]:
        """Merge legacy extras section by section, defaulting invalid sections.

        A single legacy value outside the current manager contract (for
        example an old AutoGain history below the engine minimum) must not
        block the migration of the rest, especially the SPL calibration
        profiles.
        """
        sections = {}
        if isinstance(payload, dict):
            for key in ("limiter", "headroom", "delay", "bass_enhancer",
                        "autogain", "loudness", "tone_effect"):
                section = payload.get(key)
                if not isinstance(section, dict):
                    continue
                try:
                    sections[key] = self.normalize_effects_extras({key: section})[key]
                except ValueError:
                    logger.warning("Legacy extras section %s is outside the current contract; using defaults", key)
        return self.normalize_effects_extras(sections)

    @staticmethod
    def _clean_preset_name(value: Any, fallback: str = "") -> str:
        return clean_name(value, fallback)

    @staticmethod
    def _native_preset(chain: Optional[List[dict]] = None,
                       source_presets: Optional[List[str]] = None) -> Dict[str, Any]:
        metadata = {}
        if source_presets:
            metadata["source_presets"] = list(source_presets)
        return {"schema": DSPPresetStore.SCHEMA, "version": DSPPresetStore.VERSION,
                "chain": list(chain or []), "metadata": metadata}

    def _bootstrap(self) -> None:
        direct = self._native_preset()
        neutral = self._native_preset()
        for name, payload in (("Direct", direct), ("Neutral", neutral)):
            path = self.output_dir / f"{name}.json"
            if not path.exists():
                self.preset_store.write(name, payload)
        active = self.state_store.read("active.json", {})
        if not isinstance(active, dict) or active.get("preset") not in {p["name"] for p in self.preset_store.list()}:
            self.state_store.write("active.json", {"schema": "fxroute.dsp.active", "version": 1,
                                                   "preset": "Neutral"})

    def list_presets(self) -> List[dict]:
        return self.preset_store.list(("Direct", "Neutral"))

    def list_irs(self) -> List[dict]:
        return [{"name": path.name, "basename": path.stem, "path": str(path),
                 "size": path.stat().st_size}
                for path in sorted(self.irs_dir.iterdir()) if path.is_file()]

    def get_active_preset(self) -> Optional[str]:
        state = self.state_store.read("active.json", {})
        return state.get("preset") if isinstance(state, dict) else None

    def load_preset(self, preset_name: str,
                    convolver_sample_rate_hz: Optional[int] = None) -> None:
        name = clean_name(preset_name)
        self.preset_store.read(name)
        config = self.compile_engine_config(
            [{"name": "left", "source": 0}, {"name": "right", "source": 1}],
            preset_name=name, sample_rate_hz=convolver_sample_rate_hz or 48000,
        )
        if self.apply_callback:
            self.apply_callback({"operation": "load_preset", "preset": name, "config": config})
        self.state_store.write("active.json", {"schema": "fxroute.dsp.active", "version": 1,
                                               "preset": name})
        self._seed_runtime_properties()

    def normalize_compare_state(self, compare: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        compare = compare if isinstance(compare, dict) else {}
        available = {entry["name"] for entry in self.list_presets()}
        preset_a = compare.get("presetA") if compare.get("presetA") in available else ""
        preset_b = compare.get("presetB") if compare.get("presetB") in available else ""
        if preset_a == preset_b:
            preset_b = ""
        active_side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
        if not preset_a:
            preset_a = self.get_active_preset() or ""
        if active_side == "B" and not preset_b:
            active_side = None
        if active_side is None:
            active = self.get_active_preset()
            active_side = "A" if active == preset_a else "B" if active == preset_b else None
        return {"presetA": preset_a, "presetB": preset_b, "activeSide": active_side}

    def load_compare_state(self) -> Dict[str, Any]:
        return self.normalize_compare_state(self.state_store.read("compare.json", {}))

    def save_compare_state(self, compare: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.normalize_compare_state(compare)
        self.state_store.write("compare.json", normalized)
        return normalized

    def load_global_extras(self) -> Dict[str, Any]:
        return self.normalize_effects_extras(self.state_store.read("extras.json", {}))

    def save_global_extras(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.normalize_effects_extras(extras)
        self.state_store.write("extras.json", normalized)
        return normalized

    def apply_global_extras_to_all_presets(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.save_global_extras(extras)
        updated = len([p for p in self.list_presets()
                       if p["name"] not in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS])
        self._notify_active_config()
        return {"extras": normalized, "updated": updated,
                "skipped": sorted(self.EXCLUDED_GLOBAL_EXTRAS_PRESETS)}

    def apply_global_extras_to_active_preset(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.save_global_extras(extras)
        active = self.get_active_preset()
        skipped = [active] if active in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS else []
        if not skipped:
            self._notify_active_config()
        return {"extras": normalized, "updated": 0 if skipped else 1, "skipped": skipped}

    def apply_autogain_loudness_runtime(self, previous_extras: Dict[str, Any],
                                         extras: Dict[str, Any], *,
                                         persist_all_presets: bool = True) -> Dict[str, Any]:
        previous = self.normalize_effects_extras(previous_extras)
        normalized = self.normalize_effects_extras(extras)
        if self.runtime_transition_callback:
            return self.runtime_transition_callback(previous, normalized, persist_all_presets)
        self.apply_runtime_properties_from_extras(normalized)
        return (self.apply_global_extras_to_all_presets(normalized) if persist_all_presets
                else self.apply_global_extras_to_active_preset(normalized))

    def apply_loudness_strength_runtime(self, previous_extras: Dict[str, Any],
                                        extras: Dict[str, Any]) -> Dict[str, Any]:
        return self.apply_autogain_loudness_runtime(previous_extras, extras)

    def apply_runtime_properties_from_extras(self, extras: Dict[str, Any]) -> None:
        """Write the live AutoGain/Loudness work point into the runtime readback.

        Called after a guarded transition has confirmed the candidate engine
        state (and by the rollback path with the previous extras), so
        read_loudness_runtime()/read_autogain_runtime() always report the
        confirmed state instead of stale seeded values.
        """
        normalized = self.normalize_effects_extras(extras)
        autogain = self._autogain_plugin_payload(normalized["autogain"])
        loudness = self._loudness_plugin_payload(normalized["loudness"], normalized["autogain"])
        for plugin, values in (("autogain", autogain), ("loudness", loudness)):
            for name, value in values.items():
                if name in {"bypass", "target", "fft", "volume", "output-gain"}:
                    runtime_name = "outputGain" if name == "output-gain" else name
                    self.set_active_plugin_property(plugin, 0, runtime_name, value)

    def loudness_transition_guard_db(self, previous_extras: Dict[str, Any],
                                     candidate_extras: Dict[str, Any]) -> float:
        """Guard gain for a Loudness runtime transition, with no positive jump.

        The canonical volume (master_gain stage) is constant across the
        transition; the plugin net trim is 0 dB on both sides, so the guard
        only has to keep the rebuild pin at least the guard margin below the
        previous level (clamped at 0 dB so a positive plugin trim can never
        shallow the pin).
        """
        previous = self.normalize_effects_extras(previous_extras)
        candidate = self.normalize_effects_extras(candidate_extras)
        old_payload = self._loudness_plugin_payload(previous["loudness"], previous["autogain"])
        new_payload = self._loudness_plugin_payload(candidate["loudness"], candidate["autogain"])
        return max(self.LOUDNESS_OUTPUT_GAIN_MIN_DB,
                   min(0.0, float(old_payload["output-gain"]), float(new_payload["output-gain"]))
                   - self.LOUDNESS_STRENGTH_GUARD_DB)

    def apply_temporary_effects_runtime(self, previous_extras: Dict[str, Any],
                                        extras: Dict[str, Any]) -> None:
        if not self.temporary_runtime_transition_callback:
            raise RuntimeError("Temporary native DSP transition is unavailable")
        self.temporary_runtime_transition_callback(
            self.normalize_effects_extras(previous_extras),
            self.normalize_effects_extras(extras))

    def set_loudness_volume_db(self, volume_db: float) -> Dict[str, Any]:
        previous = self.load_global_extras()
        current = copy.deepcopy(previous)
        current["loudness"]["params"]["volumeDb"] = max(-80.0, min(0.0, float(volume_db)))
        return self.apply_autogain_loudness_runtime(previous, current, persist_all_presets=False)

    @staticmethod
    def _property_key(plugin_name: str, instance_id: int, property_name: str) -> str:
        return f"{plugin_name}#{int(instance_id)}.{property_name}"

    def _seed_runtime_properties(self) -> None:
        extras = self.load_global_extras()
        values = {
            "autogain#0.target": self._autogain_plugin_payload(extras["autogain"])["target"],
            "autogain#0.bypass": self._autogain_plugin_payload(extras["autogain"])["bypass"],
            "loudness#0.volume": self._loudness_plugin_payload(extras["loudness"], extras["autogain"])["volume"],
            "loudness#0.outputGain": self._loudness_plugin_payload(extras["loudness"], extras["autogain"])["output-gain"],
            "loudness#0.bypass": self._loudness_plugin_payload(extras["loudness"], extras["autogain"])["bypass"],
        }
        for key, value in values.items():
            self._runtime_properties.setdefault(key, value)
        self.state_store.write("runtime.json", self._runtime_properties)

    def set_active_plugin_property(self, plugin_name: str, instance_id: int,
                                   property_name: str, value: int | float | bool) -> None:
        if not isinstance(value, bool):
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("DSP runtime property must be finite")
        event = {"operation": "set_property", "plugin": plugin_name,
                 "instance": int(instance_id), "property": property_name, "value": value}
        if self.apply_callback:
            self.apply_callback(event)
        self._runtime_properties[self._property_key(plugin_name, instance_id, property_name)] = value
        self.state_store.write("runtime.json", self._runtime_properties)

    def get_active_plugin_property(self, plugin_name: str, instance_id: int,
                                   property_name: str) -> str:
        key = self._property_key(plugin_name, instance_id, property_name)
        if key not in self._runtime_properties:
            raise KeyError(f"Unknown DSP runtime property: {key}")
        value = self._runtime_properties[key]
        return str(value).lower() if isinstance(value, bool) else format(float(value), ".15g")

    def read_loudness_runtime(self) -> Dict[str, Any]:
        return {"volume": float(self.get_active_plugin_property("loudness", 0, "volume")),
                "output_gain": float(self.get_active_plugin_property("loudness", 0, "outputGain")),
                "bypass": self.get_active_plugin_property("loudness", 0, "bypass") == "true"}

    def read_autogain_runtime(self) -> Dict[str, Any]:
        return {"target": float(self.get_active_plugin_property("autogain", 0, "target")),
                "bypass": self.get_active_plugin_property("autogain", 0, "bypass") == "true"}

    def _notify_active_config(self) -> None:
        if self.apply_callback:
            config = self.compile_engine_config(
                [{"name": "left", "source": 0}, {"name": "right", "source": 1}])
            self.apply_callback({"operation": "configure", "config": config})

    def _validate_supported_chain(self, chain: List[dict]) -> None:
        for plugin in chain:
            if plugin.get("type") not in self.SUPPORTED_PLUGINS:
                raise UnsupportedPluginError(f"Unsupported DSP plugin: {plugin.get('type')}")

    def compile_engine_config(self, output_layout: List[Dict[str, Any]], *,
                               preset_name: Optional[str] = None,
                               sample_rate_hz: int = 48000,
                               extras_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be a positive integer")
        if not isinstance(output_layout, list) or not output_layout:
            raise ValueError("output_layout must be a non-empty array")
        outputs = []
        names = set()
        for index, channel in enumerate(output_layout):
            if not isinstance(channel, dict) or not isinstance(channel.get("name"), str) or not channel["name"]:
                raise ValueError(f"output_layout[{index}].name is required")
            if channel["name"] in names:
                raise ValueError(f"Duplicate output name: {channel['name']}")
            names.add(channel["name"])
            routes = channel.get("routes")
            if routes is None:
                routes = [{"input": channel.get("source"), "gain": 1.0}]
            if not isinstance(routes, list) or not routes:
                raise ValueError(f"output_layout[{index}].routes must be a non-empty array")
            normalized_routes = []
            for route_index, route in enumerate(routes):
                source = route.get("input") if isinstance(route, dict) else None
                gain = route.get("gain", 1.0) if isinstance(route, dict) else None
                if not isinstance(source, int) or source < 0:
                    raise ValueError(f"output_layout[{index}].routes[{route_index}].input must be a non-negative integer")
                gain = float(gain)
                if not math.isfinite(gain):
                    raise ValueError(f"output_layout[{index}].routes[{route_index}].gain must be finite")
                normalized_routes.append({"input": source, "gain": gain})
            outputs.append({"name": channel["name"], "routes": normalized_routes,
                             "gain_db": float(channel.get("gain_db", 0.0)),
                             "delay_ms": float(channel.get("delay_ms", 0.0)),
                             "invert": bool(channel.get("invert", False)),
                             "filters": copy.deepcopy(channel.get("filters", []))})
        active = clean_name(preset_name or self.get_active_preset())
        payload = self.preset_store.read(active)
        chain = copy.deepcopy(payload["chain"])
        self._validate_supported_chain(chain)
        if active not in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
            chain.extend(self._extras_chain(
                self.normalize_effects_extras(extras_override)
                if extras_override is not None else self.load_global_extras()))
        self._validate_supported_chain(chain)
        return {"schema": self.ENGINE_SCHEMA, "version": self.ENGINE_VERSION,
                "sample_rate_hz": sample_rate_hz, "preset": active,
                 "outputs": outputs, "chain": chain}

    def compile_engine_text(self, output_layout: List[Dict[str, Any]], *,
                            preset_name: Optional[str] = None,
                            sample_rate_hz: int = 48000,
                            extras_override: Optional[Dict[str, Any]] = None) -> str:
        config = self.compile_engine_config(
            output_layout, preset_name=preset_name, sample_rate_hz=sample_rate_hz,
            extras_override=extras_override)
        max_input = max(route["input"] for output in config["outputs"] for route in output["routes"])
        lines = [f"rate {sample_rate_hz}", f"inputs {max_input + 1}", f"outputs {len(config['outputs'])}"]
        for output_index, output in enumerate(config["outputs"]):
            for route in output["routes"]:
                lines.append(f"matrix {output_index} {route['input']} {route['gain']:.9g}")
            for filter_def in output.get("filters", []):
                for _ in range(int(filter_def.get("stages", 1))):
                    lines.append("peq %d %s %.9g %.9g %.9g" % (
                        output_index, filter_def["type"], float(filter_def["frequency_hz"]),
                        float(filter_def.get("q", 0.70710678)), float(filter_def.get("gain_db", 0.0))))

        def number(value: Any) -> str:
            return format(float(value), ".9g")

        def control(name: str, value: Any) -> None:
            lines.append(f"control {name} {number(value)}")

        enabled_autogain = next((item for item in config["chain"]
                                 if item.get("enabled", True) and item["type"] == "autogain"), None)
        autogain_definition = ({"enabled": True, "params": enabled_autogain.get("params", {})}
                               if enabled_autogain else {"enabled": False, "params": {}})
        ordinal = 0
        master_gain_db = None
        lv2_uris = {
            "equalizer": "http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr",
            "bass_enhancer": "http://calf.sourceforge.net/plugins/BassEnhancer",
            "loudness": "http://lsp-plug.in/plugins/lv2/loud_comp_stereo",
            "limiter": "http://lsp-plug.in/plugins/lv2/sc_limiter_stereo",
            "maximizer": "urn:zamaudio:ZaMaximX2",
        }
        filter_types = {"bell": 1, "high_pass": 2, "high_shelf": 3,
                        "low_pass": 4, "low_shelf": 5, "notch": 6}
        eq_modes = {"IIR": 0, "FIR": 1, "FFT": 2, "SPM": 3}
        for plugin in config["chain"]:
            if not plugin.get("enabled", True):
                continue
            params = plugin.get("params", {})
            plugin_type = plugin["type"]
            peq_delay = None
            peq_gain_db = 0.0
            if plugin_type == "equalizer":
                dual = params.get("channelMode") == "dual"
                left_source = params.get("leftBands", []) if dual else params.get("bands", [])
                right_source = params.get("rightBands", []) if dual else left_source

                def split_special_bands(bands):
                    ordinary, gain_db, delay_ms = [], 0.0, 0.0
                    for band in bands:
                        if band.get("filterType") == "gain":
                            if band.get("enabled", True):
                                gain_db += float(band.get("gainDb", 0.0))
                        elif band.get("filterType") == "delay":
                            if band.get("enabled", True):
                                delay_ms += float(band.get("delayMs", 0.0))
                        else:
                            ordinary.append(band)
                    return ordinary, gain_db, delay_ms

                left, left_gain, left_delay = split_special_bands(left_source)
                right, right_gain, right_delay = split_special_bands(right_source)
                if dual and abs(left_gain - right_gain) > 1e-9:
                    if abs(left_gain) <= 1e-9:
                        peq_gain_db = right_gain
                    elif abs(right_gain) <= 1e-9:
                        peq_gain_db = left_gain
                    else:
                        raise ValueError("Gain filter supports only shared stereo trim in dual mode")
                else:
                    peq_gain_db = left_gain
                params = copy.deepcopy(params)
                if dual:
                    params["leftBands"], params["rightBands"] = left, right
                else:
                    params["bands"] = left
                if abs(left_delay) > 1e-9 or abs(right_delay) > 1e-9:
                    peq_delay = (left_delay, right_delay)
            backend = f"lv2 {lv2_uris[plugin_type]}" if plugin_type in lv2_uris else f"native {plugin_type}"
            lines.append(f"stage_begin {ordinal} {plugin.get('id', plugin_type)} {backend}")
            ordinal += 1
            if plugin_type == "equalizer":
                mix = plugin.get("mix") if isinstance(plugin.get("mix"), dict) else {}
                input_db = mix.get("inputGainDb", params.get("inputGainDb", 0.0))
                output_db = mix.get("outputGainDb", params.get("outputGainDb", 0.0))
                control("g_in", 10.0 ** ((float(input_db) + peq_gain_db) / 20.0))
                control("g_out", 10.0 ** (float(output_db) / 20.0))
                control("mode", eq_modes.get(str(params.get("eqMode", "IIR")).upper(), 0))
                dual = params.get("channelMode") == "dual"
                control("clink", 0 if dual else 1)
                left = params.get("leftBands", []) if dual else params.get("bands", [])
                right = params.get("rightBands", []) if dual else left
                for side, bands in (("l", left), ("r", right)):
                    for index in range(32):
                        band = bands[index] if index < len(bands) else None
                        if band is None:
                            control(f"ft{side}_{index}", 0)
                            continue
                        kind = str(band.get("filterType", "bell"))
                        control(f"ft{side}_{index}", filter_types.get(kind, 0)
                                if band.get("enabled", True) else 0)
                        control(f"fm{side}_{index}", 0)
                        control(f"s{side}_{index}", 0)
                        control(f"f{side}_{index}", band.get("frequencyHz", 1000.0))
                        control(f"g{side}_{index}", 10.0 ** (float(band.get("gainDb", 0.0)) / 20.0))
                        control(f"q{side}_{index}", band.get("q", 1.0))
            elif plugin_type == "convolver":
                paths = self.preset_store.find_ir_paths(str(params.get("kernel", "")))
                if not paths:
                    raise FileNotFoundError(f"IR file not found: {params.get('kernel', '')}")
                # The EasyEffects-era engine needed hidden per-rate output-gain
                # compensation (44100 +1 dB ... 768000 -24 dB).  The native
                # convolver resamples the IR and convolves at the stream rate
                # and is level-stable across rates (verified by
                # test_native_dsp_convolver_sr_level.py at 44.1/48/96/192 kHz),
                # so no compensation is applied.
                lines.append(f"param path {json.dumps(str(paths[0]))}")
                for name, default in (("wet_db", 0.0), ("dry_db", -100.0),
                                      ("input_gain_db", 0.0), ("output_gain_db", 0.0)):
                    lines.append(f"param {name} {number(params.get(name, default))}")
            elif plugin_type == "delay":
                lines.append(f"param left_ms {number(params.get('leftMs', 0.0))}")
                lines.append(f"param right_ms {number(params.get('rightMs', 0.0))}")
            elif plugin_type == "headroom":
                lines.append(f"param gain_db {number(params.get('gainDb', 0.0))}")
            elif plugin_type == "autogain":
                lines.append(f"param target_db {number(params.get('targetDb', -12.0))}")
                lines.append(f"param reference {json.dumps(str(params.get('reference', 'Geometric Mean (MSI)')))}")
                lines.append(f"param silence_threshold_db {number(params.get('silenceThresholdDb', -70.0))}")
                lines.append(f"param maximum_history_seconds {number(params.get('maximumHistorySeconds', 15))}")
            elif plugin_type == "crystalizer":
                lines.append(f"param intensity_band2_db {number(params.get('intensityBand2Db', -2.0))}")
            elif plugin_type == "bass_enhancer":
                control("listen", 0)
                control("amount", 10.0 ** (float(params.get("amount", 0.0)) / 20.0))
                control("drive", params.get("harmonics", 8.5))
                control("freq", params.get("scope", 100.0))
                control("blend", params.get("blend", 0.0))
                control("floor_active", 0)
                control("floor", 20)
            elif plugin_type == "loudness":
                definition = {"enabled": True, "params": params}
                payload = self._loudness_plugin_payload(definition, autogain_definition)
                # Enum values verified against the installed lsp-plugins
                # metadata (loud_comp_stereo.ttl): mode 0=FFT, std 4=ISO226-2023,
                # approx 2=Normal, fft map 256..16384 -> 0..6.
                control("input", 1)
                control("mode", 0)
                control("std", 4)
                control("fft", {256: 0, 512: 1, 1024: 2, 2048: 3, 4096: 4,
                                8192: 5, 16384: 6}.get(int(params.get("fftSize", 4096)), 4))
                control("approx", 2)
                control("volume", payload["volume"])
                control("hclip", 0)
                control("hcrange", 6)
                lines.append(f"param output_gain_db {number(payload['output-gain'])}")
                # The canonical listening attenuation is split off the old
                # wrapper gain (volumeDb - p): the stage keeps the LSP work
                # point p with the inverse compensation -p (level-neutral at
                # the pre-master meter tap), and the volumeDb part is applied
                # by a dedicated master gain right after the tap and before
                # the protection limiter, so the Limiter input equals the
                # pre-migration level (volumeDb) again.
                master_gain_db = float(params.get("volumeDb", 0.0))
            elif plugin_type == "limiter":
                # Control values verified against the installed lsp-plugins
                # metadata (sc_limiter_stereo.ttl): mode 0=Herm Thin (matches
                # the old EasyEffects "Herm Thin"), boost 1 = the old
                # gain-boost enabled default (plugin default is 1).  The
                # plugin ports cap at/rt/lk (0.25..20 / 0.25..20 / 0.1..20 ms);
                # the clamps below match those port bounds, so the old 50 ms
                # release is clamped to 20 ms exactly as the plugin itself did.
                control("g_in", 10.0 ** (float(params.get("inputGainDb", 0.0)) / 20.0))
                control("g_out", 10.0 ** (float(params.get("outputGainDb", 0.0)) / 20.0))
                control("scp", 1)
                control("alr", 0)
                control("alr_at", 5)
                control("alr_rt", 50)
                control("mode", 0)
                control("th", 10.0 ** (float(params.get("thresholdDb", -1.0)) / 20.0))
                control("knee", 1)
                control("smooth", -5)
                control("boost", 1)
                control("lk", max(0.1, min(20.0, float(params.get("lookaheadMs", 5.0)))))
                control("at", max(0.25, min(20.0, float(params.get("attackMs", 5.0)))))
                control("rt", max(0.25, min(20.0, float(params.get("releaseMs", 5.0)))))
                control("ovs", 0)
                control("dith", 0)
                control("extsc", 0)
                control("slink", params.get("stereoLinkPercent", 100.0))
                for name in ("in2lk", "in2sc", "lk2in", "lk2sc", "sc2in", "sc2lk"):
                    control(name, 0)
            elif plugin_type == "maximizer":
                control("gain", params.get("inputGainDb", 0.0))
                control("thresh", params.get("thresholdDb", params.get("threshold", 0.0)))
                control("rel", params.get("releaseMs", params.get("release", 25.0)))
            lines.append("stage_end")
            if master_gain_db is not None:
                lines.append(f"stage_begin {ordinal} global-loudness-master native master_gain")
                ordinal += 1
                lines.append(f"param gain_db {number(master_gain_db)}")
                lines.append("stage_end")
                master_gain_db = None
            if peq_delay is not None:
                lines.append(f"stage_begin {ordinal} {plugin.get('id', plugin_type)}-delay native delay")
                ordinal += 1
                lines.append(f"param left_ms {number(peq_delay[0])}")
                lines.append(f"param right_ms {number(peq_delay[1])}")
                lines.append("stage_end")

        for output_index, output in enumerate(config["outputs"]):
            polarity = "invert" if output["invert"] else "normal"
            lines.append(f"output {output_index} {output['gain_db']:.9g} {output['delay_ms']:.9g} {polarity}")
        lines.append(f"bypass {1 if config['preset'] == self.PURE_PRESET else 0}")
        return "\n".join(lines) + "\n"

    def _extras_chain(self, extras: Dict[str, Any]) -> List[dict]:
        normalized = self.normalize_effects_extras(extras)
        result = []
        for plugin_type in ("headroom", "delay"):
            definition = normalized[plugin_type]
            if definition.get("enabled"):
                result.append({"id": f"global-{plugin_type}", "type": plugin_type,
                               "enabled": True, "params": copy.deepcopy(definition.get("params", {}))})
        tone = normalized["tone_effect"]
        if tone.get("enabled"):
            result.append({"id": f"global-{tone['mode']}", "type": tone["mode"],
                           "enabled": True, "params": {}})
        for plugin_type in ("bass_enhancer", "autogain", "loudness", "limiter"):
            definition = normalized[plugin_type]
            if definition.get("enabled"):
                result.append({"id": f"global-{plugin_type}", "type": plugin_type,
                               "enabled": True, "params": copy.deepcopy(definition.get("params", {}))})
        return result

    def validate_peq_v1(self, definition: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(definition, dict):
            raise ValueError("peq must be an object")
        params = copy.deepcopy(definition.get("params") or {})
        mode = str(params.get("channelMode", "stereo-linked"))
        if mode not in {"stereo-linked", "dual"}:
            raise ValueError("peq.params.channelMode must be stereo-linked or dual")
        eq_mode = str(params.get("eqMode", "IIR")).upper()
        if eq_mode not in {"IIR", "FIR", "FFT", "SPM"}:
            raise ValueError("peq.params.eqMode is not supported")

        def bands(value: Any, field: str) -> List[dict]:
            if not isinstance(value, list) or len(value) > 20:
                raise ValueError(f"{field} must be an array with at most 20 bands")
            result = []
            aliases = {"pk": "bell", "bell": "bell", "notch": "notch",
                       "gain": "gain", "delay": "delay",
                       "low_shelf": "low_shelf", "high_shelf": "high_shelf",
                       "low_pass": "low_pass", "high_pass": "high_pass"}
            for index, raw in enumerate(value):
                if not isinstance(raw, dict):
                    raise ValueError(f"{field}[{index}] must be an object")
                kind = aliases.get(str(raw.get("filterType", "bell")).strip().lower().replace("-", "_"))
                if kind is None:
                    raise ValueError(f"{field}[{index}].filterType is not supported")
                frequency = float(raw.get("frequencyHz", 1000.0))
                gain = float(raw.get("gainDb", 0.0))
                q = float(raw.get("q", 1.0))
                delay = float(raw.get("delayMs", 0.0))
                if not 20 <= frequency <= 20000:
                    raise ValueError(f"{field}[{index}].frequencyHz must be between 20 and 20000")
                if not -24 <= gain <= 24 or not 0.1 <= q <= 20:
                    raise ValueError(f"{field}[{index}] gain or Q is outside the supported range")
                if kind == "delay" and not 0 <= delay <= 500:
                    raise ValueError(f"{field}[{index}].delayMs must be between 0 and 500")
                result.append({"enabled": bool(raw.get("enabled", True)), "filterType": kind,
                               "frequencyHz": frequency, "gainDb": gain, "q": q,
                               "delayMs": delay})
            return result

        if mode == "dual":
            params["leftBands"] = bands(params.get("leftBands", []), "peq.params.leftBands")
            params["rightBands"] = bands(params.get("rightBands", []), "peq.params.rightBands")
        else:
            params["bands"] = bands(params.get("bands", []), "peq.params.bands")
        params["channelMode"] = mode
        params["eqMode"] = eq_mode
        return {"enabled": bool(definition.get("enabled", True)), "params": params,
                "mix": copy.deepcopy(definition.get("mix") or {})}

    def import_rew_peq_text(self, text: str) -> Dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("REW PEQ text is empty")
        bands = []
        pattern = re.compile(
            r"^\s*\d+\s+(?:true|false|on|off)\s+(?:auto\s+)?(?:PK|PEQ)\s+"
            r"([0-9.]+)\s+([-+0-9.]+)\s+([0-9.]+)", re.IGNORECASE)
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                bands.append({"filterType": "bell", "frequencyHz": float(match.group(1)),
                              "gainDb": float(match.group(2)), "q": float(match.group(3)),
                              "enabled": True})
        if not bands:
            raise ValueError("No supported REW PEQ filters found")
        return {"source": "REW", "peq": {"enabled": True, "params": {
            "channelMode": "stereo-linked", "eqMode": "IIR", "bands": bands}}}

    def create_peq_preset(self, preset_name: str, peq_definition: Dict[str, Any],
                          extras: Optional[Dict[str, Any]] = None) -> dict:
        del extras
        normalized = self.validate_peq_v1(peq_definition)
        plugin = {"id": "equalizer#0", "type": "equalizer",
                  "enabled": normalized["enabled"],
                  "params": copy.deepcopy(normalized["params"]),
                  "mix": copy.deepcopy(normalized["mix"])}
        path = self.preset_store.write(preset_name, self._native_preset([plugin]))
        bands = normalized["params"].get("bands", [])
        return {"name": clean_name(preset_name), "filename": path.name, "path": str(path),
                "band_count": len(bands), "channel_mode": normalized["params"]["channelMode"]}

    def create_convolver_preset(self, preset_name: str, ir_filename: str,
                                extras: Optional[Dict[str, Any]] = None) -> dict:
        del extras
        if Path(ir_filename).name not in {item["name"] for item in self.list_irs()}:
            raise FileNotFoundError(f"IR file not found: {ir_filename}")
        kernel = Path(ir_filename).stem
        plugin = {"id": "convolver#0", "type": "convolver", "enabled": True,
                  "params": {"kernel": kernel, "wet_db": 0.0, "dry_db": -100.0,
                             "input_gain_db": 0.0, "output_gain_db": 0.0}}
        path = self.preset_store.write(preset_name, self._native_preset([plugin]))
        return {"name": clean_name(preset_name), "filename": path.name,
                "path": str(path), "kernel_name": kernel}

    def combine_presets(self, preset_name: str, source_presets: List[str],
                        extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        del extras
        sources = [clean_name(name) for name in source_presets] if isinstance(source_presets, list) else []
        if len(sources) < 2 or len(set(sources)) != len(sources):
            raise ValueError("Select at least two different presets to combine")
        if clean_name(preset_name) in sources:
            raise ValueError("New preset name must differ from the source presets")
        chain = []
        counters: Dict[str, int] = {}
        for source in sources:
            for plugin in self.preset_store.read(source)["chain"]:
                item = copy.deepcopy(plugin)
                base = item["type"]
                number = counters.get(base, 0)
                counters[base] = number + 1
                item["id"] = f"{base}#{number}"
                chain.append(item)
        self._validate_supported_chain(chain)
        path = self.preset_store.write(preset_name, self._native_preset(chain, sources))
        return {"name": clean_name(preset_name), "filename": path.name, "path": str(path),
                "source_presets": sources, "plugin_count": len(chain)}

    def import_preset_json(self, preset_filename: str, preset_text: str) -> Dict[str, Any]:
        try:
            source = json.loads(preset_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Preset JSON is invalid: {exc}") from exc
        if isinstance(source, dict) and source.get("schema") == self.PRESET_SCHEMA:
            payload = self.preset_store.validate(source)
            self._validate_supported_chain(payload["chain"])
        else:
            payload = self._translate_legacy_preset(source)
        name = clean_name(preset_filename)
        path = self.preset_store.write(name, payload)
        return {"name": name, "filename": path.name, "path": str(path),
                "source_presets": payload.get("metadata", {}).get("source_presets", [])}

    def export_preset_json(self, preset_name: str) -> str:
        return json.dumps(self.preset_store.read(preset_name), indent=2, sort_keys=True) + "\n"

    def _translate_legacy_preset(self, source: Any) -> Dict[str, Any]:
        output = source.get("output") if isinstance(source, dict) else None
        if not isinstance(output, dict) or not isinstance(output.get("plugins_order", []), list):
            raise ValueError("Preset JSON is neither a native nor EasyEffects output preset")
        ordered = list(output.get("plugins_order", []))
        for name, value in output.items():
            if name not in {"plugins_order", "blocklist"} and isinstance(value, dict) and name not in ordered:
                ordered.append(name)
        chain = []
        counters: Dict[str, int] = {}
        for legacy_id in ordered:
            if not isinstance(legacy_id, str) or not isinstance(output.get(legacy_id), dict):
                raise ValueError(f"Invalid legacy plugin entry: {legacy_id!r}")
            plugin_type = legacy_id.split("#", 1)[0]
            if plugin_type not in self.SUPPORTED_PLUGINS:
                raise UnsupportedPluginError(f"Unsupported imported plugin: {plugin_type}")
            raw = output[legacy_id]
            number = counters.get(plugin_type, 0)
            counters[plugin_type] = number + 1
            params = self._translate_legacy_params(plugin_type, raw)
            chain.append({"id": f"{plugin_type}#{number}", "type": plugin_type,
                          "enabled": not bool(raw.get("bypass", False)), "params": params})
        sources = source.get("fxroute", {}).get("source_presets", []) if isinstance(source.get("fxroute"), dict) else []
        return self._native_preset(chain, sources if isinstance(sources, list) else [])

    def _translate_legacy_params(self, plugin_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        if plugin_type == "delay":
            return {"leftMs": float(raw.get("time-l", 0.0)),
                    "rightMs": float(raw.get("time-r", 0.0))}
        if plugin_type == "convolver":
            kernel = raw.get("kernel-name")
            if not isinstance(kernel, str) or not kernel:
                raise ValueError("Imported convolver is missing kernel-name")
            return {"kernel": Path(kernel).stem, "wet_db": float(raw.get("wet", 0.0)),
                    "dry_db": float(raw.get("dry", -100.0)),
                    "input_gain_db": float(raw.get("input-gain", 0.0)),
                    "output_gain_db": float(raw.get("output-gain", 0.0))}
        if plugin_type == "equalizer":
            split = bool(raw.get("split-channels", False))
            params: Dict[str, Any] = {"channelMode": "dual" if split else "stereo-linked",
                                      "eqMode": str(raw.get("mode", "IIR")).upper()}
            left = self._legacy_eq_bands(raw.get("left", {}), raw.get("num-bands", 0))
            right = self._legacy_eq_bands(raw.get("right", {}), raw.get("num-bands", 0))
            if split:
                params.update({"leftBands": left, "rightBands": right})
            else:
                params["bands"] = left
            params["inputGainDb"] = float(raw.get("input-gain", 0.0))
            params["outputGainDb"] = float(raw.get("output-gain", 0.0))
            return params
        aliases = {"threshold": "thresholdDb", "attack": "attackMs", "release": "releaseMs",
                   "lookahead": "lookaheadMs", "stereo-link": "stereoLinkPercent",
                   "target": "targetDb", "maximum-history": "maximumHistorySeconds",
                   "silence-threshold": "silenceThresholdDb", "fft": "fftSize",
                   "volume": "volumeDb", "output-gain": "outputGainDb"}
        translated = {aliases.get(key, key): copy.deepcopy(value) for key, value in raw.items()
                      if key not in {"bypass", "input-gain"}}
        if plugin_type == "limiter":
            translated["inputGainDb"] = float(raw.get("input-gain", 0.0))
            translated["outputGainDb"] = float(raw.get("output-gain", 0.0))
            translated["lookaheadMs"] = max(0.1, min(20.0, float(translated.get("lookaheadMs", 5.0))))
            translated["attackMs"] = max(0.25, min(20.0, float(translated.get("attackMs", 5.0))))
        elif plugin_type == "maximizer":
            translated["inputGainDb"] = float(raw.get("input-gain", 0.0))
        return translated

    @staticmethod
    def _legacy_eq_bands(channel: Any, count: Any) -> List[dict]:
        if not isinstance(channel, dict):
            raise ValueError("Imported equalizer channel must be an object")
        result = []
        type_map = {"Bell": "bell", "Notch": "notch", "Lo-shelf": "low_shelf",
                    "Hi-shelf": "high_shelf", "Lo-pass": "low_pass", "Hi-pass": "high_pass"}
        for index in range(max(0, int(count))):
            band = channel.get(f"band{index}")
            if not isinstance(band, dict):
                raise ValueError(f"Imported equalizer is missing band{index}")
            legacy_type = band.get("type", "Bell")
            if legacy_type not in type_map:
                raise UnsupportedPluginError(f"Unsupported imported equalizer filter: {legacy_type}")
            result.append({"enabled": not bool(band.get("mute", False)),
                           "filterType": type_map[legacy_type],
                           "frequencyHz": float(band.get("frequency", 1000.0)),
                           "gainDb": float(band.get("gain", 0.0)),
                           "q": float(band.get("q", 1.0))})
        return result

    def delete_preset(self, preset_name: str) -> None:
        name = clean_name(preset_name)
        if name in self.PROTECTED_PRESETS:
            raise ValueError(f'Preset "{name}" is a built-in preset and cannot be deleted')
        payload = self.preset_store.read(name)
        kernels = self.preset_store.kernels(payload)
        self.preset_store.path(name).unlink()
        orphaned = kernels - self.preset_store.referenced_kernels_except(name)
        for kernel in orphaned:
            for path in self.preset_store.find_ir_paths(kernel):
                path.unlink()
        if self.get_active_preset() == name:
            self.load_preset("Neutral")

    def _extract_kernel_names_from_payload(self, payload: Optional[Dict[str, Any]]):
        if isinstance(payload, dict) and payload.get("schema") == self.PRESET_SCHEMA:
            return self.preset_store.kernels(payload)
        try:
            return self.preset_store.kernels(self._translate_legacy_preset(payload))
        except (ValueError, UnsupportedPluginError):
            return set()

    def _find_ir_paths_for_kernel_name(self, kernel_name: str) -> List[Path]:
        return self.preset_store.find_ir_paths(kernel_name)

    def upload_ir(self, source_path: Path, filename: str,
                  stored_name: Optional[str] = None) -> dict:
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"IR upload not found: {source}")
        name = Path(stored_name or filename).name
        if not name.lower().endswith((".irs", ".wav")):
            raise ValueError("IR file must be .irs or .wav")
        destination = self.irs_dir / name
        shutil.copyfile(source, destination)
        return {"name": destination.name, "basename": destination.stem,
                "path": str(destination), "size": destination.stat().st_size}

    def create_convolver_preset_with_upload(self, preset_name: str, source_path: Path,
                                            filename: str, extras=None) -> dict:
        uploaded = self.upload_ir(source_path, filename,
                                  f"{clean_name(preset_name, 'convolver')}{Path(filename).suffix}")
        return {"ir": uploaded,
                "preset": self.create_convolver_preset(preset_name, uploaded["name"], extras)}

    def upload_ir_pair(self, left_source_path: Path, left_filename: str,
                       right_source_path: Path, right_filename: str,
                       merged_name: str) -> dict:
        del left_filename, right_filename
        with wave.open(str(left_source_path), "rb") as left, wave.open(str(right_source_path), "rb") as right:
            left_format = (left.getnchannels(), left.getsampwidth(), left.getframerate(), left.getnframes())
            right_format = (right.getnchannels(), right.getsampwidth(), right.getframerate(), right.getnframes())
            if left_format[0] != 1 or right_format[0] != 1:
                raise ValueError("Dual IR inputs must be mono WAV files")
            if left_format[1:] != right_format[1:]:
                raise ValueError("Dual IR WAV formats must match")
            left_frames = left.readframes(left.getnframes())
            right_frames = right.readframes(right.getnframes())
        width = left_format[1]
        stereo = bytearray()
        for offset in range(0, len(left_frames), width):
            stereo.extend(left_frames[offset:offset + width])
            stereo.extend(right_frames[offset:offset + width])
        destination = self.irs_dir / Path(merged_name).name
        with wave.open(str(destination), "wb") as output:
            output.setparams((2, width, left_format[2], left_format[3], "NONE", ""))
            output.writeframes(stereo)
        return {"name": destination.name, "basename": destination.stem,
                "path": str(destination), "size": destination.stat().st_size}

    def create_convolver_preset_with_dual_uploads(
        self, preset_name: str, left_source_path: Path, left_filename: str,
        right_source_path: Path, right_filename: str, extras=None,
    ) -> dict:
        merged_name = f"{clean_name(preset_name, 'dual-convolver')}.irs"
        uploaded = self.upload_ir_pair(left_source_path, left_filename,
                                       right_source_path, right_filename, merged_name)
        return {"ir": uploaded,
                "preset": self.create_convolver_preset(preset_name, uploaded["name"], extras)}

    def create_peq_preset_from_rew_text(self, preset_name: str, rew_text: str,
                                        extras=None) -> Dict[str, Any]:
        imported = self.import_rew_peq_text(rew_text)
        return {**self.create_peq_preset(preset_name, imported["peq"], extras),
                "import_source": imported["source"]}

    def create_dual_peq_preset_from_rew_texts(self, preset_name: str,
                                              left_rew_text: str, right_rew_text: str,
                                              extras=None) -> Dict[str, Any]:
        left = self.import_rew_peq_text(left_rew_text)
        right = self.import_rew_peq_text(right_rew_text)
        definition = {"enabled": True, "params": {"channelMode": "dual",
                      "leftBands": left["peq"]["params"]["bands"],
                      "rightBands": right["peq"]["params"]["bands"]}}
        return {**self.create_peq_preset(preset_name, definition, extras),
                "import_source": {"left": left["source"], "right": right["source"]}}

    def active_preset_requires_samplerate_reload(self,
                                                 sample_rate_hz: Optional[int] = None) -> bool:
        del sample_rate_hz
        payload = self.preset_store.read(self.get_active_preset() or "Direct")
        return any(plugin["type"] == "convolver" and plugin.get("enabled", True)
                   for plugin in payload["chain"])

    def get_status(self) -> dict:
        presets = self.list_presets()
        return {"available": True, "mode": "native", "runtime": {"owner": "fxroute"},
                "preset_count": len(presets), "active_preset": self.get_active_preset(),
                "presets": presets, "irs": self.list_irs(), "compare": self.load_compare_state(),
                "global_extras": self.load_global_extras(),
                "global_extras_excluded_presets": sorted(self.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
                "paths": {"output": str(self.output_dir), "irs": str(self.irs_dir),
                          "state": str(self.state_dir), "global_extras": str(self.global_extras_file),
                          "compare_state": str(self.compare_state_file)}}


# Naming bridge for call sites transitioning from EasyEffectsManager.
NativeDSPManager = DSPManager
