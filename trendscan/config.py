"""Central settings. Every other module gets configuration from here.

Settings come from environment variables (optionally loaded from a local .env file).
The API key is read here and nowhere else, and is never printed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

# Load .env from the project root into os.environ (does not override variables
# that are already set in the shell).
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    gemini_model: str = "gemini-3.5-flash-lite"
    max_analyses_per_scan: int = 30   # protects the free-tier daily quota
    seconds_between_calls: float = 12  # ~5 requests/minute; measured: 7 s caused frequent 429 retries
    db_path: Path = DATA_DIR / "trendscan.db"
    sources_file: Path = CONFIG_DIR / "sources.yaml"
    profiles_file: Path = CONFIG_DIR / "trend_profiles.yaml"
    # repr=False keeps the key out of print(settings), logs and tracebacks.
    gemini_api_key: str | None = field(default=None, repr=False)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        gemini_model=env.get("TRENDSCAN_GEMINI_MODEL", Settings.gemini_model),
        max_analyses_per_scan=int(env.get("TRENDSCAN_MAX_ANALYSES_PER_SCAN", Settings.max_analyses_per_scan)),
        seconds_between_calls=float(env.get("TRENDSCAN_SECONDS_BETWEEN_CALLS", Settings.seconds_between_calls)),
        db_path=Path(env.get("TRENDSCAN_DB_PATH", Settings.db_path)),
        gemini_api_key=env.get("GEMINI_API_KEY") or None,
    )


def load_sources(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


def load_profiles(path: Path) -> tuple[int, list[dict]]:
    """Return (version, profiles)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return int(data.get("version", 1)), data.get("profiles", [])
