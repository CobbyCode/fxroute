"""Configuration management using pydantic-settings."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Keys that live in .env but are consumed by install.sh / systemd helpers
# rather than by the Python Settings model.  They are still valid .env keys
# and must not be reported as typos.
INSTALLER_MANAGED_ENV_KEYS = frozenset({
    "SPOTIFY_AUTOSTART",
    "SPOTIFY_CACHE_CLEANUP",
    "SPOTIFY_CACHE_CLEANUP_INTERVAL_HOURS",
    "SYSTEM_AUTO_UPDATE",
    "SYSTEM_AUTO_UPDATE_INTERVAL_HOURS",
})

# Application settings read directly from the process environment (not via
# the Settings model) that may also legitimately appear in .env, where
# systemd's EnvironmentFile injects them for the app.
DIRECT_ENV_KEYS = frozenset({
    "FXROUTE_DSP_BINARY",
    "FXROUTE_RADIO_BROWSER_URL",
    "MUSIC_LIBRARY_SMB_HOSTS",
})


def _normalize_log_level(level: str | None) -> str:
    raw_level = str(level or "INFO").strip().upper()
    aliases = {
        "WARN": "WARNING",
        "VERBOSE": "DEBUG",
    }
    return aliases.get(raw_level, raw_level)


def setup_logging(level: str = "INFO"):
    """Configure stdout logging."""
    normalized_level = _normalize_log_level(level)
    logging.basicConfig(
        level=getattr(logging, normalized_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Required
    MUSIC_ROOT: Path = Field(..., description="Absolute path to music library root")

    # Optional with defaults
    DOWNLOADS_SUBDIR: str = Field("incoming", description="Subdirectory for downloads")
    DOWNLOAD_TRANSCODE_FORMAT: Optional[str] = Field(
        None,
        description="Optional transcode format for URL downloads; leave unset to preserve the source format whenever possible",
    )
    AUDIO_FORMAT: Optional[str] = Field(
        None,
        description="Deprecated legacy transcode setting for URL downloads; kept only for backwards compatibility",
    )
    LOG_LEVEL: str = Field("INFO", description="Logging level")
    MAX_DOWNLOADS: int = Field(1, description="Maximum concurrent downloads (V1=1)")
    HOST: str = Field("0.0.0.0", description="Bind host")
    PORT: int = Field(8000, description="Bind port")
    HARDWARE_CONTROLLER_DEVICE: Optional[str] = Field(
        None,
        description="Optional device path for the external hardware controller",
    )

    # Derived
    @property
    def download_dir(self) -> Path:
        """Full path to download directory."""
        return self.MUSIC_ROOT / self.DOWNLOADS_SUBDIR

    @field_validator("DOWNLOAD_TRANSCODE_FORMAT", "AUDIO_FORMAT", mode="before")
    @classmethod
    def normalize_download_format(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            cleaned = v.strip().lower()
            return cleaned or None
        return str(v).strip().lower() or None

    @property
    def download_transcode_format(self) -> Optional[str]:
        explicit = self.DOWNLOAD_TRANSCODE_FORMAT
        if explicit in {None, "original", "source", "native", "keep", "none", "off", "best"}:
            explicit = None
        if explicit:
            return explicit

        legacy = self.AUDIO_FORMAT
        if legacy in {None, "", "original", "source", "native", "keep", "none", "off", "best", "mp3"}:
            return None
        return legacy

    @field_validator("MUSIC_ROOT", mode="before")
    @classmethod
    def expand_music_root(cls, v):
        """Expand ~ and environment variables like $HOME before Path validation."""
        if isinstance(v, Path):
            v = str(v)
        if isinstance(v, str):
            return Path(os.path.expandvars(os.path.expanduser(v))).resolve(strict=False)
        return v

    @field_validator("MUSIC_ROOT")
    @classmethod
    def validate_music_root(cls, v: Path) -> Path:
        """Ensure MUSIC_ROOT is absolute and exists."""
        if not v.is_absolute():
            raise ValueError("MUSIC_ROOT must be an absolute path")
        if not v.exists():
            # We'll warn but not fail - maybe it will be created
            logging.warning(f"MUSIC_ROOT does not exist: {v}")
        return v

    @field_validator("PORT")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Ensure port is in valid range."""
        if not (1 <= v <= 65535):
            raise ValueError("PORT must be between 1 and 65535")
        return v


# Global settings instance (initialized at startup)
settings: Optional[Settings] = None


def _parse_env_file_keys(env_file: Path) -> set[str]:
    """Return the set of keys declared in an env file, ignoring comments.

    Only ``KEY=VALUE`` lines count (an optional ``export `` prefix is
    accepted).  This is deliberately a plain-text scan so keys pydantic
    drops via ``extra="ignore"`` can still be surfaced.
    """
    if not env_file.is_file():
        return set()
    keys: set[str] = set()
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def warn_unknown_env_file_keys(env_file: Path | None = None) -> None:
    """Warn about .env keys the application does not interpret.

    ``extra="ignore"`` keeps pydantic from failing on unknown keys, but a
    misspelled setting (e.g. ``PORTS=8001``) would otherwise be dropped
    silently.  Every key in the env file must be either a ``Settings`` field
    or a documented installer-managed key; anything else is reported as a
    likely typo at WARNING level.
    """
    path = Path(env_file or ".env")
    keys = _parse_env_file_keys(path)
    if not keys:
        return
    known = {name.lower() for name in Settings.model_fields}
    known.update(name.lower() for name in INSTALLER_MANAGED_ENV_KEYS)
    known.update(name.lower() for name in DIRECT_ENV_KEYS)
    unknown = sorted({key for key in keys if key.lower() not in known})
    if unknown:
        logging.warning(
            "Unknown .env keys are ignored by FXRoute (check for typos): %s",
            ", ".join(unknown),
        )


def get_settings() -> Settings:
    """Get or create the global settings instance.

    This is the single authoritative access point for application
    configuration.  ``main.settings`` is only an alias of this same cached
    instance and must not be re-instantiated elsewhere.
    """
    global settings
    if settings is None:
        setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
        try:
            settings = Settings()
            setup_logging(settings.LOG_LEVEL)
            warn_unknown_env_file_keys()
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")
            # Show friendly error on stderr and exit
            print(f"Configuration error: {e}", file=sys.stderr)
            print("Please check your .env file and ensure MUSIC_ROOT is set correctly.", file=sys.stderr)
            sys.exit(1)
    return settings
