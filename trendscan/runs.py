"""Run a scan safely from anywhere (UI button, command line, scheduler).

Two guarantees on top of `scan.run_scan`:

1. Only one scan at a time. Two scans in parallel would write the same database and double
   the Gemini request rate (→ HTTP 429). A lock file next to the database enforces this. The
   operating system releases the lock when the process ends, even after a crash, so it can
   never get stuck.
2. Every scan is recorded in the `scan_runs` table, so results of a scheduled scan are visible
   in the inbox later, and a scan that crashed or was stopped shows up as such.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Callable

from .analysis import GeminiAnalyzer
from .config import Settings
from .scan import ScanReport, run_scan
from .storage import Store

TRIGGERS = ("manual", "scheduled")


class ScanBusy(Exception):
    """Another scan holds the lock."""


def lock_path(settings: Settings) -> Path:
    return settings.db_path.with_suffix(".lock")


@contextmanager
def scan_lock(path: Path):
    """Exclusive, non-blocking lock on `path`. Raises ScanBusy if another scan holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            _lock(fh)
        except OSError:
            raise ScanBusy("Another scan is already running.") from None
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


if os.name == "nt":   # Windows
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:                 # macOS, Linux
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def settings_for(trigger: str, settings: Settings) -> Settings:
    """Scheduled scans run unattended: give slow PDF servers more time. Gemini caps stay the same."""
    if trigger == "scheduled":
        return replace(settings,
                       pdf_timeout_seconds=settings.scheduled_pdf_timeout_seconds,
                       max_doc_seconds_per_scan=settings.scheduled_doc_seconds_per_scan)
    return settings


def run_and_record(settings: Settings, store: Store, trigger: str,
                   analyzer: GeminiAnalyzer | None = None,
                   progress: Callable[[str], None] = lambda msg: None,
                   sleep: Callable[[float], None] = time.sleep) -> ScanReport:
    """Take the lock, run one scan, and record it. Raises ScanBusy if a scan is already running."""
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}")
    with scan_lock(lock_path(settings)):
        run_id = store.start_run(trigger)
        try:
            report = run_scan(settings_for(trigger, settings), store, analyzer=analyzer,
                              progress=progress, sleep=sleep)
        except Exception as exc:
            store.finish_run(run_id, "error", None, f"{type(exc).__name__}: {exc}"[:300])
            raise
        except BaseException:          # Ctrl+C, process stopped
            store.finish_run(run_id, "interrupted", None)
            raise
        store.finish_run(run_id, "ok", asdict(report))
        return report


def report_from_dict(data: dict) -> ScanReport:
    """Rebuild a stored report; ignores fields that a later version of TrendScan no longer has."""
    known = {f.name for f in fields(ScanReport)}
    return ScanReport(**{k: v for k, v in data.items() if k in known})
