"""Command line for TrendScan.

    python -m trendscan scan                     run one scan now (same as the button in the app)
    python -m trendscan scan --scheduled         what the scheduler runs (longer PDF time limits)
    python -m trendscan schedule install --time 07:00 [--weekdays]
    python -m trendscan schedule status
    python -m trendscan schedule remove
    python -m trendscan history                  last scans and their results
    python -m trendscan digest [--preview]       email digest of what is new (test mode: saved to a file)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from . import schedule
from .config import load_profiles, load_settings
from .digest import DigestConfigError, run_digest
from .runs import ScanBusy, run_and_record
from .storage import Store

log = logging.getLogger("trendscan")

EXIT_OK, EXIT_ERROR, EXIT_BUSY = 0, 1, 75     # 75 = "temporary failure, try again later"


class RedactSecret(logging.Filter):
    """Replace secrets with *** in every log line, in case a library echoes one in an error."""

    def __init__(self, *secrets: str | None):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        hits = [s for s in self.secrets if s in message]
        for secret in hits:
            message = message.replace(secret, "***")
        if hits:
            record.msg, record.args = message, None
        return True


def setup_logging(*secrets: str | None) -> None:
    schedule.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(schedule.LOG_DIR / "scan.log", maxBytes=1_000_000,
                                       backupCount=3, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(RedactSecret(*secrets))
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def cmd_scan(args) -> int:
    settings = load_settings()
    setup_logging(settings.gemini_api_key, settings.smtp_password)
    trigger = "scheduled" if args.scheduled else "manual"
    store = Store(settings.db_path)
    log.info("%s scan started (model %s, API key %s)", trigger.capitalize(), settings.gemini_model,
             "set" if settings.gemini_configured else "NOT set - analyses stay pending")
    try:
        r = run_and_record(settings, store, trigger, progress=log.info)
    except ScanBusy:
        log.warning("Another scan is already running. Nothing to do.")
        return EXIT_BUSY
    except Exception:
        log.exception("Scan failed")
        return EXIT_ERROR
    log.info("Scan finished: fetched=%d new=%d pdfs_read=%d gemini_calls=%d reused=%d pending=%d "
             "failed=%d new_versions=%d", r.fetched, r.new, r.docs_fetched, r.gemini_calls,
             r.cache_hits, r.pending, r.failed, r.versions_added)
    for name, err in r.source_errors.items():
        log.warning("Source unavailable: %s - %s", name, err)
    if r.gemini_stopped:
        log.warning("Gemini paused: %s", r.gemini_stopped)
    if args.scheduled:
        make_digest(settings, store)      # a failing digest is logged; the scan itself succeeded
    return EXIT_OK


def make_digest(settings, store, preview: bool = False) -> bool:
    _, profiles = load_profiles(settings.profiles_file)
    try:
        digest, location = run_digest(settings, store, profiles, preview=preview)
    except DigestConfigError as exc:
        log.error("Digest not sent: %s", exc)
        return False
    except Exception:
        log.exception("Digest failed")
        return False
    if location is None:
        log.info("Digest: nothing new to report.")
    elif preview:
        log.info("Digest preview (%d entries, nothing remembered as reported): %s", len(digest.entries), location)
    elif settings.email_mode == "test":
        log.info("Digest saved (test mode, not sent; %d entries): %s", len(digest.entries), location)
    else:
        log.info("Digest sent to %s (%d entries)", location, len(digest.entries))
    return True


def cmd_digest(args) -> int:
    settings = load_settings()
    setup_logging(settings.gemini_api_key, settings.smtp_password)
    return EXIT_OK if make_digest(settings, Store(settings.db_path), preview=args.preview) else EXIT_ERROR


def cmd_schedule(args) -> int:
    if args.action == "install":
        try:
            hour, minute = schedule.parse_time(args.time)
            path = schedule.install(hour, minute, args.weekdays)
        except (ValueError, RuntimeError) as exc:
            print(f"Error: {exc}")
            return EXIT_ERROR
        days = "Monday to Friday" if args.weekdays else "every day"
        print(f"Scheduled scan installed: {days} at {hour:02d}:{minute:02d} (local time).\n"
              f"Schedule file: {path}\nLog: {schedule.LOG_DIR / 'scan.log'}")
    elif args.action == "remove":
        print("Scheduled scan removed." if schedule.remove() else "No scheduled scan was installed.")
    else:
        st = schedule.status()
        if st is None:
            print("No scheduled scan installed." if schedule.supported()
                  else "Automatic scheduling is only implemented for macOS. See README.")
        else:
            days = "Monday to Friday" if st["weekdays_only"] else "every day"
            print(f"Scheduled scan: {days} at {st['time']} · "
                  f"{'active' if st['loaded'] else 'installed but NOT loaded'}\n{st['path']}")
    return EXIT_OK


def cmd_history(args) -> int:
    store = Store(load_settings().db_path)
    runs = store.recent_runs(args.limit)
    if not runs:
        print("No scans recorded yet.")
    for run in runs:
        r = run["report"] or {}
        numbers = (f"new={r.get('new', 0)} pdfs={r.get('docs_fetched', 0)} "
                   f"gemini={r.get('gemini_calls', 0)} pending={r.get('pending', 0)}") if r else ""
        started = datetime.fromisoformat(run["started_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"#{run['run_id']:<4} {started}  {run['trigger']:<9} {run['status']:<11} "
              f"{numbers}{run['error'] or ''}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trendscan", description="TrendScan command line")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="run one scan now")
    p.add_argument("--scheduled", action="store_true",
                   help="unattended run: longer PDF time limits, recorded as 'scheduled'")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("schedule", help="manage the daily scheduled scan (macOS)")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("--time", default="07:00", help="local time, e.g. 07:00 (default)")
    p.add_argument("--weekdays", action="store_true", help="Monday to Friday only")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("history", help="show the last scans")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("digest", help="email digest of what is new since the last digest")
    p.add_argument("--preview", action="store_true",
                   help="write data/digests/preview.html only; nothing is remembered as reported")
    p.set_defaults(func=cmd_digest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
