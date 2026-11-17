"""Scheduled scans: lock, scan history, scheduled settings, launchd file, command line, log redaction.

Run:  .venv/bin/python -m pytest tests/test_runs.py -v
"""

from __future__ import annotations

import logging
import plistlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from trendscan import __main__ as cli
from trendscan import runs as runs_module
from trendscan import scan as scan_module
from trendscan import schedule
from trendscan.analysis import GeminiAnalyzer
from trendscan.config import load_settings
from trendscan.runs import ScanBusy, lock_path, report_from_dict, run_and_record, scan_lock
from trendscan.storage import Store

from test_scan import FakeInteractions, make_item


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=tmp_path / "t.db", seconds_between_calls=0,
                       gemini_api_key=None)
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "test", "name": "Test feed", "type": "rss", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: [make_item(1), make_item(2)])
    return settings, Store(settings.db_path)


def fake_analyzer(settings):
    return GeminiAnalyzer(None, settings.gemini_model,
                          client=SimpleNamespace(interactions=FakeInteractions()))


def test_scan_is_recorded_with_its_report(env):
    settings, store = env
    run_and_record(settings, store, "manual", analyzer=fake_analyzer(settings))
    [run] = store.recent_runs()
    assert (run["trigger"], run["status"], run["error"]) == ("manual", "ok", None)
    assert run["finished_at"] is not None
    report = report_from_dict(run["report"])
    assert (report.new, report.gemini_calls) == (2, 2)


def test_second_scan_is_refused_while_one_is_running(env):
    settings, store = env
    with scan_lock(lock_path(settings)):            # another process/thread is scanning
        with pytest.raises(ScanBusy):
            run_and_record(settings, store, "scheduled")
    assert store.recent_runs() == []                # the refused scan did nothing
    run_and_record(settings, store, "manual")       # lock released -> scanning works again
    assert store.recent_runs()[0]["status"] == "ok"


def test_lock_is_released_when_a_scan_crashes(env, monkeypatch):
    settings, store = env

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(scan_module, "check_revisions", boom)
    with pytest.raises(RuntimeError):
        run_and_record(settings, store, "scheduled")
    [run] = store.recent_runs()
    assert run["status"] == "error" and "disk full" in run["error"]

    monkeypatch.undo()                              # problem fixed
    monkeypatch.setattr(scan_module, "load_sources", lambda _: [])
    run_and_record(settings, store, "manual")       # not blocked by the crashed scan
    assert [r["status"] for r in store.recent_runs()] == ["ok", "error"]


def test_scan_killed_mid_run_is_marked_interrupted(env):
    settings, store = env
    store.start_run("scheduled")                    # process was killed: row stays "running"
    run_and_record(settings, store, "manual")
    assert [r["status"] for r in store.recent_runs()] == ["ok", "interrupted"]


def test_scheduled_scan_gets_longer_pdf_limits_but_same_gemini_caps(env, monkeypatch):
    settings, store = env
    seen = []

    def fake_run_scan(s, store, **kwargs):
        seen.append(s)
        return scan_module.ScanReport()
    monkeypatch.setattr(runs_module, "run_scan", fake_run_scan)

    run_and_record(settings, store, "scheduled")
    s = seen[-1]
    assert s.pdf_timeout_seconds == settings.scheduled_pdf_timeout_seconds == 300
    assert s.max_doc_seconds_per_scan == settings.scheduled_doc_seconds_per_scan == 1200
    assert s.max_analyses_per_scan == settings.max_analyses_per_scan

    run_and_record(settings, store, "manual")
    assert seen[-1].pdf_timeout_seconds == settings.pdf_timeout_seconds == 120


def test_launchd_file_runs_the_scheduled_scan_and_contains_no_secret():
    raw = schedule.build_plist(7, 30, weekdays_only=True, python="/p/.venv/bin/python",
                               project_root=Path("/p"))
    data = plistlib.loads(raw)
    assert data["ProgramArguments"] == ["/p/.venv/bin/python", "-m", "trendscan", "scan", "--scheduled"]
    assert data["WorkingDirectory"] == "/p"
    assert [d["Weekday"] for d in data["StartCalendarInterval"]] == [1, 2, 3, 4, 5]
    assert all((d["Hour"], d["Minute"]) == (7, 30) for d in data["StartCalendarInterval"])
    assert data["RunAtLoad"] is False
    assert b"GEMINI" not in raw and b"AIza" not in raw     # only paths, never the key

    daily = plistlib.loads(schedule.build_plist(6, 0))
    assert daily["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}


@pytest.mark.parametrize("text", ["7", "24:00", "07:60", "seven"])
def test_invalid_schedule_time_is_rejected(text):
    with pytest.raises(ValueError):
        schedule.parse_time(text)


def test_api_key_is_redacted_from_log_lines(caplog):
    logger = logging.getLogger("trendscan.redact-test")
    handler_filter = cli.RedactSecret("AIza-secret-123")
    caplog.handler.addFilter(handler_filter)
    try:
        logger.warning("request failed: url?key=%s", "AIza-secret-123")
    finally:
        caplog.handler.removeFilter(handler_filter)
    assert "AIza-secret-123" not in caplog.text and "key=***" in caplog.text


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Command line against a temp DB and temp log folder, with a fake feed and no API key."""
    monkeypatch.setenv("TRENDSCAN_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(schedule, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "test", "name": "Test feed", "type": "rss", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: [make_item(1)])
    logger = logging.getLogger("trendscan")
    before = list(logger.handlers)
    yield tmp_path
    for h in logger.handlers[len(before):]:
        logger.removeHandler(h)
        h.close()


def test_command_line_scan_records_a_scheduled_run_and_logs_to_file(cli_env):
    assert cli.main(["scan", "--scheduled"]) == cli.EXIT_OK
    [run] = Store(cli_env / "cli.db").recent_runs()
    assert (run["trigger"], run["status"]) == ("scheduled", "ok")
    assert run["report"]["pending"] == 1               # no key: collected, analysis pending
    text = (cli_env / "logs" / "scan.log").read_text()
    assert "Scheduled scan started" in text and "Scan finished" in text


def test_command_line_scan_exits_75_when_busy(cli_env):
    settings = load_settings()
    with scan_lock(lock_path(settings)):
        assert cli.main(["scan"]) == cli.EXIT_BUSY
