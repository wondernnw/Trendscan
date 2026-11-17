"""Central settings. Every other module gets configuration from here.

Settings come from environment variables (optionally loaded from a local .env file).
The API key and the mail password are read here and nowhere else, and are never printed or logged.
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
    # Full text of papers and reports (PDF)
    max_doc_chars: int = 60_000        # text sent to Gemini per document (whole pages, from page 1)
    max_fulltext_per_scan: int = 10    # full-text analyses (and PDF downloads) per scan
    max_pdf_mb: int = 25               # larger PDFs are skipped and flagged
    pdf_timeout_seconds: float = 120   # time limit for one PDF download
    max_doc_seconds_per_scan: float = 300   # time budget for all PDF downloads in one scan
    revision_check_days: int = 90      # look for new versions of items first seen in this many days
    # Scheduled scans run unattended, so slow PDF servers can be given more time.
    scheduled_pdf_timeout_seconds: float = 300
    scheduled_doc_seconds_per_scan: float = 1200
    db_path: Path = DATA_DIR / "trendscan.db"
    sources_file: Path = CONFIG_DIR / "sources.yaml"
    profiles_file: Path = CONFIG_DIR / "trend_profiles.yaml"
    # Email digest. "test" saves each digest to data/digests/ and sends nothing;
    # "smtp" sends it through the mail server below.
    email_mode: str = "test"
    email_to: tuple[str, ...] = ()
    email_from: str = "TrendScan <trendscan@localhost>"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    digest_dir: Path = DATA_DIR / "digests"
    # repr=False keeps secrets out of print(settings), logs and tracebacks.
    gemini_api_key: str | None = field(default=None, repr=False)
    smtp_password: str | None = field(default=None, repr=False)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        gemini_model=env.get("TRENDSCAN_GEMINI_MODEL", Settings.gemini_model),
        max_analyses_per_scan=int(env.get("TRENDSCAN_MAX_ANALYSES_PER_SCAN", Settings.max_analyses_per_scan)),
        seconds_between_calls=float(env.get("TRENDSCAN_SECONDS_BETWEEN_CALLS", Settings.seconds_between_calls)),
        max_doc_chars=int(env.get("TRENDSCAN_MAX_DOC_CHARS", Settings.max_doc_chars)),
        max_fulltext_per_scan=int(env.get("TRENDSCAN_MAX_FULLTEXT_PER_SCAN", Settings.max_fulltext_per_scan)),
        max_pdf_mb=int(env.get("TRENDSCAN_MAX_PDF_MB", Settings.max_pdf_mb)),
        pdf_timeout_seconds=float(env.get("TRENDSCAN_PDF_TIMEOUT_SECONDS", Settings.pdf_timeout_seconds)),
        max_doc_seconds_per_scan=float(env.get("TRENDSCAN_MAX_DOC_SECONDS_PER_SCAN", Settings.max_doc_seconds_per_scan)),
        revision_check_days=int(env.get("TRENDSCAN_REVISION_CHECK_DAYS", Settings.revision_check_days)),
        scheduled_pdf_timeout_seconds=float(env.get("TRENDSCAN_SCHEDULED_PDF_TIMEOUT_SECONDS",
                                                    Settings.scheduled_pdf_timeout_seconds)),
        scheduled_doc_seconds_per_scan=float(env.get("TRENDSCAN_SCHEDULED_DOC_SECONDS_PER_SCAN",
                                                     Settings.scheduled_doc_seconds_per_scan)),
        db_path=Path(env.get("TRENDSCAN_DB_PATH", Settings.db_path)),
        email_mode=env.get("TRENDSCAN_EMAIL_MODE", Settings.email_mode).strip().lower(),
        email_to=tuple(a.strip() for a in env.get("TRENDSCAN_EMAIL_TO", "").split(",") if a.strip()),
        email_from=env.get("TRENDSCAN_EMAIL_FROM", Settings.email_from),
        smtp_host=env.get("TRENDSCAN_SMTP_HOST") or None,
        smtp_port=int(env.get("TRENDSCAN_SMTP_PORT", Settings.smtp_port)),
        smtp_user=env.get("TRENDSCAN_SMTP_USER") or None,
        gemini_api_key=env.get("GEMINI_API_KEY") or None,
        smtp_password=env.get("TRENDSCAN_SMTP_PASSWORD") or None,
    )


def load_sources(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


def load_profiles(path: Path) -> tuple[int, list[dict]]:
    """Return (version, profiles)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return int(data.get("version", 1)), data.get("profiles", [])
