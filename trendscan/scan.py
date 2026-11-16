"""One manual scan: fetch sources -> store items -> analyse what needs analysing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .analysis import AnalysisFailed, GeminiAnalyzer, GeminiUnavailable, cache_key
from .config import Settings, load_profiles, load_sources
from .sources import fetch_source
from .storage import Store

log = logging.getLogger("trendscan")


@dataclass
class ScanReport:
    fetched: int = 0
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    source_errors: dict[str, str] = field(default_factory=dict)
    gemini_calls: int = 0          # actual API requests made in this scan
    cache_hits: int = 0            # items whose stored analysis was reused
    analysed: int = 0              # successful new analyses
    failed: int = 0                # per-item failures (will be retried next scan)
    pending: int = 0               # still waiting for analysis after this scan
    gemini_stopped: str | None = None   # why Gemini calls stopped early, if they did


def run_scan(settings: Settings, store: Store, analyzer: GeminiAnalyzer | None = None,
             progress: Callable[[str], None] = lambda msg: None,
             sleep: Callable[[float], None] = time.sleep) -> ScanReport:
    report = ScanReport()
    profiles_version, profiles = load_profiles(settings.profiles_file)
    if analyzer is None:
        analyzer = GeminiAnalyzer(settings.gemini_api_key, settings.gemini_model)

    # 1) Collect. A failing source is recorded and skipped; the others still run.
    for source in load_sources(settings.sources_file):
        progress(f"Fetching {source['name']} …")
        try:
            items = fetch_source(source)
        except Exception as exc:
            report.source_errors[source["name"]] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for item in items:
            report.fetched += 1
            outcome = store.upsert_item(item)   # "new" | "unchanged" | "changed"
            setattr(report, outcome, getattr(report, outcome) + 1)

    # 2) Decide what needs Gemini. We look at ALL stored items, so items left pending
    #    by an earlier scan (quota, outage) are picked up again automatically.
    to_analyse = []
    for row in store.inbox():
        item = store.get_item(row["item_id"])
        key = cache_key(item, profiles_version, settings.gemini_model)
        if row["analysis_status"] == "done" and row["cache_key"] == key:
            report.cache_hits += 1       # unchanged item -> reuse, no API call
        else:
            to_analyse.append((item, key))

    # 3) Analyse, newest first, within the per-scan cap, with throttling.
    for n, (item, key) in enumerate(to_analyse[: settings.max_analyses_per_scan]):
        if report.gemini_stopped:
            break
        if n > 0:
            sleep(settings.seconds_between_calls)
        progress(f"Analysing {n + 1}/{min(len(to_analyse), settings.max_analyses_per_scan)}: {item.title[:60]} …")
        try:
            if analyzer.client is not None:
                report.gemini_calls += 1
            result = analyzer.analyze(item, profiles)
            store.save_analysis(item.item_id, key, settings.gemini_model, result=result)
            report.analysed += 1
        except GeminiUnavailable as exc:
            report.gemini_stopped = str(exc)
            log.warning("Gemini unavailable: %s", exc)
        except AnalysisFailed as exc:
            store.save_analysis(item.item_id, key, settings.gemini_model, error=str(exc))
            report.failed += 1

    report.pending = len(to_analyse) - report.analysed - report.failed
    return report
