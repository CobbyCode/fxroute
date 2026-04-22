"""Configuration management using pydantic-settings."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def setup_logging(level: str = "INFO"):
    """Configure stdout logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
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


def get_settings() -> Settings:
    """Get or create global settings instance."""
    global settings
    if settings is None:
        setup_logging()
        try:
            settings = Settings()
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")
            # Show friendly error on stderr and exit
            print(f"Configuration error: {e}", file=sys.stderr)
            print("Please check your .env file and ensure MUSIC_ROOT is set correctly.", file=sys.stderr)
            sys.exit(1)
    return settings
